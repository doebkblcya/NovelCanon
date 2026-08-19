"""结构化查询（阶段 05 最小闭环；阶段 10 扩展）。

只读 active run 的数据；knowledge_cutoff_chapter 过滤 observed 时间；
所有回答返回 evidence 与章节定位（chapter_id/ordinal/source span）。

定版契约（验收 P1）：
- 每次检索必须先限定 book_id：QueryService 构造时绑定，所有公开方法生效；
- 默认查询只返回「当前 supported、非 retract」的事实：
  每个 fact 取最新版本（窗口 rn=1），再过滤 operation != 'retract' 且
  claim_status = 'supported'（最新版本是 retract 时该 fact 无当前值，不回溯）。
"""

from __future__ import annotations

from sqlalchemy import Engine, text


def _cutoff_sql(cutoff: int | None) -> tuple[str, dict[str, object]]:
    if cutoff is None:
        return "", {}
    return "AND c.observed_ordinal <= :cutoff", {"cutoff": cutoff}


def _current_filter() -> str:
    """每 fact 最新版本（rn=1）后过滤 retract / 非 supported。"""
    return "q.rn = 1 AND q.operation != 'retract' AND q.claim_status = 'supported'"


class QueryService:
    """基于 active 视图的结构化查询（book_id 绑定，多书隔离）。"""

    def __init__(self, engine: Engine, book_id: str) -> None:
        self._engine = engine
        self._book_id = book_id

    # ── canonical 展开（阶段 08：mention → canonical 投影解析）────

    def entity_scope(self, canonical_id: str) -> list[str]:
        """把 canonical 展开为查询用实体集合：canonical 自身 + 名下全部 mention_id。

        阶段 07 materialize 时 claim 实体字段引用 mention_id（章级
        namespace），阶段 08 消歧后 canonical 是唯一查询入口——查询层
        经 entity_resolutions 投影展开，历史 claim 不改写（08 §6）。
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT mention_id FROM entity_resolutions WHERE canonical_id = :c"
                ),
                {"c": canonical_id},
            ).fetchall()
        scope = [canonical_id] + [r[0] for r in rows]
        # 去重：canonical 自身可能也是某个 mention（首披露实体作锚）
        return list(dict.fromkeys(scope))

    @staticmethod
    def _scope_sql(
        scope: list[str], prefix: str = "e"
    ) -> tuple[str, dict[str, object]]:
        """IN 子句（实体集合展开）。prefix 区分多组占位符。"""
        if not scope:
            return "1=0", {}
        placeholders = ", ".join(f":{prefix}{i}" for i in range(len(scope)))
        return f"IN ({placeholders})", {f"{prefix}{i}": e for i, e in enumerate(scope)}

    def display_name(self, canonical_id: str, *, knowledge_cutoff: int | None = None) -> str | None:
        """某章截止前已披露的展示名（§9.1：只能来自截止前的 alias claim）。"""
        cutoff_sql = ""
        params: dict[str, object] = {}
        if knowledge_cutoff is not None:
            cutoff_sql = "AND a.observed_ordinal <= :cutoff"
            params["cutoff"] = knowledge_cutoff
        params["cid"] = canonical_id
        params["book"] = self._book_id
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT a.surface_name FROM entity_alias_claims a"
                    " JOIN alias_observations o ON o.claim_version_id = a.claim_version_id"
                    " JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                    " WHERE a.canonical_id = :cid AND r.status = 'active' AND r.book_id = :book"
                    f" {cutoff_sql}"
                    " ORDER BY a.observed_ordinal DESC, a.rowid DESC LIMIT 1"
                ),
                params,
            ).fetchone()
            return row[0] if row else None

    def current_state(
        self,
        canonical_id: str,
        field: str,
        *,
        knowledge_cutoff: int | None = None,
    ) -> dict | None:
        """实体某状态字段的当前值 + 证据（active 中每 fact 最新版本）。"""
        for state in self.entity_state(canonical_id, knowledge_cutoff=knowledge_cutoff):
            if state["field"] == field:
                return state
        return None

    def entity_state(self, canonical_id: str, *, knowledge_cutoff: int | None = None) -> list[dict]:
        """实体全部状态字段的当前版本（每 fact 最新 + supported + 非 retract）。"""
        cutoff_sql, params = _cutoff_sql(knowledge_cutoff)
        scope_sql, scope_params = self._scope_sql(self.entity_scope(canonical_id))
        params.update(scope_params)
        params["book"] = self._book_id
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT q.claim_version_id, q.fact_id, q.field, q.value, q.raw_value,"
                        " q.observed_ordinal"
                        " FROM ("
                        "  SELECT c.claim_version_id, c.fact_id, c.observed_ordinal,"
                        "         c.operation, c.claim_status,"
                        "         s.field, s.value, s.raw_value,"
                        "         ROW_NUMBER() OVER (PARTITION BY c.fact_id"
                        "           ORDER BY c._rowid DESC) rn"
                        "  FROM v_active_claims c"
                        "  JOIN state_claims s ON s.claim_version_id = c.claim_version_id"
                        "  WHERE s.subject_entity_id " + scope_sql + " AND c.book_id = :book"
                        f"  {cutoff_sql}"
                        ") q"
                        f" WHERE {_current_filter()}"
                    ),
                    params,
                )
                .mappings()
                .fetchall()
            )
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = self._evidence_for(d["claim_version_id"])
            out.append(d)
        return out

    def one_hop_relations(
        self, canonical_id: str, *, knowledge_cutoff: int | None = None
    ) -> list[dict]:
        """一跳关系（from/to 任一为该实体）+ 证据（active，每 fact 当前版本）。"""
        cutoff_sql, params = _cutoff_sql(knowledge_cutoff)
        scope = self.entity_scope(canonical_id)
        from_sql, from_params = self._scope_sql(scope, prefix="f")
        to_sql, to_params = self._scope_sql(scope, prefix="t")
        params.update(from_params)
        params.update(to_params)
        params["book"] = self._book_id
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT q.claim_version_id, q.fact_id, q.observed_ordinal,"
                        " q.from_entity_id, q.to_entity_id, q.relation_type, q.relation_raw"
                        " FROM ("
                        "  SELECT c.claim_version_id, c.fact_id, c.observed_ordinal,"
                        "         c.operation, c.claim_status,"
                        "         r.from_entity_id, r.to_entity_id, r.relation_type,"
                        "         r.relation_raw,"
                        "         ROW_NUMBER() OVER (PARTITION BY c.fact_id"
                        "           ORDER BY c._rowid DESC) rn"
                        "  FROM v_active_claims c"
                        "  JOIN relation_claims r ON r.claim_version_id = c.claim_version_id"
                        "  WHERE (r.from_entity_id " + from_sql + " OR r.to_entity_id "
                        + to_sql + ") AND c.book_id = :book"
                        f"  {cutoff_sql}"
                        ") q"
                        f" WHERE {_current_filter()}"
                        " ORDER BY q.observed_ordinal"
                    ),
                    params,
                )
                .mappings()
                .fetchall()
            )
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = self._evidence_for(d["claim_version_id"])
            out.append(d)
        return out

    def event_participants(self, event_claim_version_id: str) -> dict | None:
        """事件参与者（§5.2）+ 事件证据与章节定位。

        仅当事件版本满足全部默认事实条件时返回：
        - 属于当前书 active run；
        - 是本书该 fact 的当前版本（active 中最新）；
        - operation != 'retract' 且 claim_status = 'supported'。
        返回 {"event": {...}, "participants": [...]}；否则 None。
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT c.claim_version_id, c.observed_chapter_id, c.observed_ordinal,"
                        " e.event_type, e.summary, e.location_entity_id"
                        " FROM event_claims e"
                        " JOIN claims c ON c.claim_version_id = e.claim_version_id"
                        " JOIN claim_observations o ON o.claim_version_id = c.claim_version_id"
                        " JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                        " WHERE e.claim_version_id = :e AND r.status = 'active'"
                        "   AND r.book_id = :book"
                        "   AND c.operation != 'retract' AND c.claim_status = 'supported'"
                        "   AND c.claim_version_id = ("
                        "       SELECT c2.claim_version_id FROM claims c2"
                        "       JOIN claim_observations o2 ON o2.claim_version_id ="
                        "            c2.claim_version_id"
                        "       JOIN extraction_runs r2 ON r2.run_id = o2.extraction_run_id"
                        "       WHERE c2.fact_id = c.fact_id AND r2.status = 'active'"
                        "         AND r2.book_id = :book"
                        "       ORDER BY c2.rowid DESC LIMIT 1)"
                    ),
                    {"e": event_claim_version_id, "book": self._book_id},
                )
                .mappings()
                .fetchone()
            )
            if row is None:
                return None
            event = dict(row)
            event["evidence"] = self._evidence_for(event["claim_version_id"])
            participants = (
                conn.execute(
                    text(
                        "SELECT p.entity_id, p.role, ent.canonical_name"
                        " FROM event_participants p"
                        " JOIN entities ent ON ent.canonical_id = p.entity_id"
                        " WHERE p.event_claim_version_id = :e"
                    ),
                    {"e": event_claim_version_id},
                )
                .mappings()
                .fetchall()
            )
            return {"event": event, "participants": [dict(p) for p in participants]}

    def claim_history(self, fact_id: str) -> list[dict]:
        """某 fact 的完整版本历史（append-only，按写入序；限定本书）。"""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT c.claim_version_id, c.operation, c.supersedes_version_id,"
                        " c.claim_status, c.observed_ordinal, c.created_by_run_id"
                        " FROM claims c"
                        " JOIN chapters ch ON c.observed_chapter_id = ch.chapter_id"
                        " WHERE c.fact_id = :f AND ch.book_id = :book ORDER BY c.rowid"
                    ),
                    {"f": fact_id, "book": self._book_id},
                )
                .mappings()
                .fetchall()
            )
        return [dict(r) for r in rows]

    def chapter_citation(self, claim_version_id_value: str) -> dict | None:
        """回答附带的章节定位（chapter_id/ordinal/source span，05 验证项）。"""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT c.observed_chapter_id, c.observed_ordinal,"
                        " e.chapter_id AS evidence_chapter, e.char_start, e.char_end, e.span_hash"
                        " FROM claims c"
                        " JOIN chapters ch ON c.observed_chapter_id = ch.chapter_id"
                        " LEFT JOIN claim_evidence e ON e.claim_version_id = c.claim_version_id"
                        " WHERE c.claim_version_id = :v AND ch.book_id = :book LIMIT 1"
                    ),
                    {"v": claim_version_id_value, "book": self._book_id},
                )
                .mappings()
                .fetchone()
            )
            return dict(row) if row else None

    def _evidence_for(self, claim_version_id_value: str) -> list[dict]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT evidence_id, evidence_stance, chapter_id, char_start, char_end,"
                        " span_hash FROM claim_evidence WHERE claim_version_id = :v"
                    ),
                    {"v": claim_version_id_value},
                )
                .mappings()
                .fetchall()
            )
        return [dict(r) for r in rows]
