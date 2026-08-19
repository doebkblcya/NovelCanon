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


def _world_sql(world_at: int | None) -> tuple[str, dict[str, object]]:
    """world at chapter 的世界有效区间过滤（09 §7，P1 扩展）。

    story_time：world_valid_from <= chapter <= world_valid_to（to NULL 持续）；
    chapter_proxy：world_valid_from <= chapter（章节近似）；
    unknown：不返回（世界时间未知，不能表达为精确状态）。
    与 knowledge_cutoff 是两个独立参数，各自进入过滤（双时间组合）。
    """
    if world_at is None:
        return "", {}
    return (
        " AND ("
        "   (c.world_valid_kind = 'story_time'"
        "    AND c.world_valid_from <= :world"
        "    AND (c.world_valid_to IS NULL OR c.world_valid_to >= :world))"
        "   OR (c.world_valid_kind = 'chapter_proxy'"
        "    AND c.world_valid_from <= :world)"
        " )",
        {"world": world_at},
    )


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
        """某章截止前已披露的展示名（§9.1：只能来自截止前的 alias claim）。

        P0 修复：materialize 写入的 alias.canonical_id 是章级 mention 实体
        （消歧前的临时实体），展示名查询经 entity_resolutions 投影——
        COALESCE(er.canonical_id, a.canonical_id) 才是该 alias 对应的最终
        canonical。真实 materialize→resolve→activate 流程无需手工补写
        canonical alias 即可返回展示名（历史 claim 不改写，查询层投影）。
        """
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
                    " LEFT JOIN entity_resolutions er ON er.mention_id = a.canonical_id"
                    " WHERE COALESCE(er.canonical_id, a.canonical_id) = :cid"
                    " AND r.status = 'active' AND r.book_id = :book"
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
        self,
        canonical_id: str,
        *,
        knowledge_cutoff: int | None = None,
        world_at: int | None = None,
    ) -> list[dict]:
        """一跳关系（from/to 任一为该实体）+ 证据（active，每 fact 当前版本）。

        双时间（P1 扩展）：knowledge_cutoff（读者知识）与 world_at
        （世界时间）两个独立参数同时过滤——世界有效区间未覆盖 world_at
        的关系不返回，observed 晚于 cutoff 的关系不返回。
        """
        cutoff_sql, cutoff_params = _cutoff_sql(knowledge_cutoff)
        world_sql, world_params = _world_sql(world_at)
        scope = self.entity_scope(canonical_id)
        from_sql, from_params = self._scope_sql(scope, prefix="f")
        to_sql, to_params = self._scope_sql(scope, prefix="t")
        params: dict[str, object] = {}
        params.update(cutoff_params)
        params.update(world_params)
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
                        "         c.operation, c.claim_status, c.world_valid_kind,"
                        "         r.from_entity_id, r.to_entity_id, r.relation_type,"
                        "         r.relation_raw,"
                        "         ROW_NUMBER() OVER (PARTITION BY c.fact_id"
                        "           ORDER BY c._rowid DESC) rn"
                        "  FROM v_active_claims c"
                        "  JOIN relation_claims r ON r.claim_version_id = c.claim_version_id"
                        "  WHERE (r.from_entity_id " + from_sql + " OR r.to_entity_id "
                        + to_sql + ") AND c.book_id = :book"
                        f"  {cutoff_sql}"
                        f"  {world_sql}"
                        ") q"
                        f" WHERE {_current_filter()}"
                        "   AND q.world_valid_kind != 'unknown'"
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

    # ── 因果递归查询（09 §5）───────────────────────────────────

    def causal_paths(
        self,
        event_claim_version_id: str,
        *,
        knowledge_cutoff: int | None = None,
        world_at: int | None = None,
        max_depth: int = 5,
    ) -> list[dict]:
        """从某事件沿 event_links 递归展开因果链（causes/enables）。

        - 递归 CTE：visited 集合阻止环；
        - 默认最大深度 5；
        - 路径置信度 = 边置信度乘积；
        - 只走 supported 边；任一端证据在 cutoff 后不可见时截断该分支
          （09 §4「任一端证据在 cutoff 后不可见时，该边不可用于回答」）；
        - 双时间（P1）：world_at 与 knowledge_cutoff 两个独立参数同时过滤
          ——图谱边按 world_valid 区间（chapter_proxy：from <= world；
          story_time：from <= world <= to）过滤，与读者披露独立；
        - 多路径按置信度降序（09 §5）。
        """
        cutoff_sql = ""
        world_sql = ""
        params: dict[str, object] = {
            "start": event_claim_version_id,
            "book": self._book_id,
            "depth": max_depth,
        }
        if knowledge_cutoff is not None:
            cutoff_sql = "AND l.observed_ordinal <= :cutoff"
            params["cutoff"] = knowledge_cutoff
        if world_at is not None:
            world_sql = (
                " AND ("
                "   (l.world_valid_kind = 'chapter_proxy'"
                "    AND l.world_valid_from <= :world)"
                "   OR (l.world_valid_kind = 'story_time'"
                "    AND l.world_valid_from <= :world"
                "    AND (l.world_valid_to IS NULL OR l.world_valid_to >= :world))"
                " )"
            )
            params["world"] = world_at
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "WITH RECURSIVE causal(src, tgt, path, depth, conf, visited) AS ("
                        "  SELECT l.source_event_id, l.target_event_id,"
                        "         l.source_event_id || '>' || l.target_event_id,"
                        "         1, l.confidence,"
                        "         '[' || l.source_event_id || ',' || l.target_event_id || ']'"
                        "  FROM event_links l"
                        "  JOIN event_link_observations o"
                        "    ON o.claim_version_id = l.claim_version_id"
                        "  JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                        "  JOIN event_link_verifications v"
                        "    ON v.claim_version_id = l.claim_version_id"
                        "   AND v.extraction_run_id = r.run_id"
                        "  WHERE l.source_event_id = :start AND r.status = 'active'"
                        "    AND r.book_id = :book AND v.claim_status = 'supported'"
                        f"    {cutoff_sql}{world_sql}"
                        "  UNION ALL"
                        "  SELECT c.src, l.target_event_id,"
                        "         c.path || '>' || l.target_event_id,"
                        "         c.depth + 1, c.conf * l.confidence,"
                        "         c.visited || ',' || l.target_event_id"
                        "  FROM causal c"
                        "  JOIN event_links l ON l.source_event_id = c.tgt"
                        "  JOIN event_link_observations o"
                        "    ON o.claim_version_id = l.claim_version_id"
                        "  JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                        "  JOIN event_link_verifications v"
                        "    ON v.claim_version_id = l.claim_version_id"
                        "   AND v.extraction_run_id = r.run_id"
                        "  WHERE r.status = 'active' AND r.book_id = :book"
                        "    AND v.claim_status = 'supported'"
                        f"    {cutoff_sql}{world_sql}"
                        "    AND c.depth < :depth"
                        "    AND instr(c.visited, l.target_event_id) = 0"
                        ")"
                        " SELECT src, tgt, path, depth, conf"
                        " FROM causal ORDER BY conf DESC"
                    ),
                    params,
                )
                .mappings()
                .fetchall()
            )
        out = []
        for r in rows:
            d = dict(r)
            d["event"] = self._event_summary(d["tgt"])
            out.append(d)
        return out

    def _event_summary(self, event_claim_version_id: str) -> dict | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT e.claim_version_id, e.event_type, e.summary,"
                        " e.sequence_in_chapter, c.observed_ordinal"
                        " FROM event_claims e"
                        " JOIN claims c ON c.claim_version_id = e.claim_version_id"
                        " JOIN chapters ch ON c.observed_chapter_id = ch.chapter_id"
                        " WHERE e.claim_version_id = :v AND ch.book_id = :book"
                    ),
                    {"v": event_claim_version_id, "book": self._book_id},
                )
                .mappings()
                .fetchone()
            )
            return dict(row) if row else None

    # ── results 反向因果查询（09 §3：results = causes 的反向）────

    def causal_results(
        self,
        event_claim_version_id: str,
        *,
        knowledge_cutoff: int | None = None,
        world_at: int | None = None,
        max_depth: int = 5,
    ) -> list[dict]:
        """从某事件沿 event_links **反向**找 causes 来源（results 查询）。

        results 通过 causes 的反向查询得到（09 §3，不单独存储）：
        沿「target_event_id = 当前事件」且 relation_type = 'causes' 的边
        反向展开（被什么原因导致）。与 causal_paths 同构：visited 防环、
        深度上限、置信度乘积、supported 边、cutoff 截断。

        P1 修复：递归 CTE 初始分支与递归分支都必须限定 relation_type =
        'causes'——enables/prevents 不是 causes 的反向结果，不得混入。
        P1：world_at（图谱边 world_valid 区间）与 knowledge_cutoff 双参数
        同时过滤。
        """
        cutoff_sql = ""
        world_sql = ""
        params: dict[str, object] = {
            "start": event_claim_version_id,
            "book": self._book_id,
            "depth": max_depth,
        }
        if knowledge_cutoff is not None:
            cutoff_sql = "AND l.observed_ordinal <= :cutoff"
            params["cutoff"] = knowledge_cutoff
        if world_at is not None:
            world_sql = (
                " AND ("
                "   (l.world_valid_kind = 'chapter_proxy'"
                "    AND l.world_valid_from <= :world)"
                "   OR (l.world_valid_kind = 'story_time'"
                "    AND l.world_valid_from <= :world"
                "    AND (l.world_valid_to IS NULL OR l.world_valid_to >= :world))"
                " )"
            )
            params["world"] = world_at
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "WITH RECURSIVE causes(src, tgt, path, depth, conf, visited) AS ("
                        "  SELECT l.source_event_id, l.target_event_id,"
                        "         l.source_event_id || '<' || l.target_event_id,"
                        "         1, l.confidence,"
                        "         '[' || l.source_event_id || ',' || l.target_event_id || ']'"
                        "  FROM event_links l"
                        "  JOIN event_link_observations o"
                        "    ON o.claim_version_id = l.claim_version_id"
                        "  JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                        "  JOIN event_link_verifications v"
                        "    ON v.claim_version_id = l.claim_version_id"
                        "   AND v.extraction_run_id = r.run_id"
                        "  WHERE l.target_event_id = :start AND r.status = 'active'"
                        "    AND r.book_id = :book AND v.claim_status = 'supported'"
                        "    AND l.relation_type = 'causes'"
                        f"    {cutoff_sql}{world_sql}"
                        "  UNION ALL"
                        "  SELECT l.source_event_id, c.tgt,"
                        "         l.source_event_id || '<' || c.path,"
                        "         c.depth + 1, c.conf * l.confidence,"
                        "         l.source_event_id || ',' || c.visited"
                        "  FROM causes c"
                        "  JOIN event_links l ON l.target_event_id = c.src"
                        "  JOIN event_link_observations o"
                        "    ON o.claim_version_id = l.claim_version_id"
                        "  JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                        "  JOIN event_link_verifications v"
                        "    ON v.claim_version_id = l.claim_version_id"
                        "   AND v.extraction_run_id = r.run_id"
                        "  WHERE r.status = 'active' AND r.book_id = :book"
                        "    AND v.claim_status = 'supported'"
                        "    AND l.relation_type = 'causes'"
                        f"    {cutoff_sql}{world_sql}"
                        "    AND c.depth < :depth"
                        "    AND instr(c.visited, l.source_event_id) = 0"
                        ")"
                        " SELECT src, tgt, path, depth, conf"
                        " FROM causes ORDER BY conf DESC"
                    ),
                    params,
                )
                .mappings()
                .fetchall()
            )
        out = []
        for r in rows:
            d = dict(r)
            d["event"] = self._event_summary(d["src"])
            out.append(d)
        return out

    # ── world at chapter（09 §7，与 knowledge cutoff 独立）──────

    def world_state_at(
        self,
        canonical_id: str,
        chapter_ordinal: int,
        *,
        knowledge_cutoff: int | None = None,
    ) -> list[dict]:
        """某世界时间点（章节序）实体的可见状态（双时间组合查询）。

        world at chapter（09 §7，P0 修复：不再用 observed_ordinal 冒充
        世界时间）：
        - story_time：world_valid_from <= chapter <= world_valid_to
          （to 为 NULL 表示持续生效）；
        - chapter_proxy：world_valid_from <= chapter（章节近似世界时间，
          明确标注为近似）；
        - unknown：不返回（world time 未知，不能表达为精确状态）。

        双时间组合（验收 P0）：knowledge_cutoff 与 world_at 两个独立参数
        同时进入过滤——「世界时间从第 1 章成立、但第 10 章才通过回忆披露」
        的事实，读者 cutoff=5 时不得看到（observed_ordinal <= cutoff 必须
        成立），world_at 与 cutoff 互不替代、组合生效。
        """
        cutoff_sql, cutoff_params = _cutoff_sql(knowledge_cutoff)
        scope_sql, scope_params = self._scope_sql(self.entity_scope(canonical_id))
        params = dict(scope_params)
        params.update(cutoff_params)
        params["book"] = self._book_id
        params["chapter"] = chapter_ordinal
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT q.claim_version_id, q.fact_id, q.field, q.value,"
                        " q.observed_ordinal, q.world_valid_kind FROM ("
                        " SELECT c.claim_version_id, c.fact_id, c.observed_ordinal,"
                        " c.operation, c.claim_status, c.world_valid_kind,"
                        " s.field, s.value,"
                        " ROW_NUMBER() OVER (PARTITION BY c.fact_id"
                        "   ORDER BY c._rowid DESC) rn"
                        " FROM v_active_claims c"
                        " JOIN state_claims s ON s.claim_version_id = c.claim_version_id"
                        " WHERE s.subject_entity_id " + scope_sql
                        + " AND c.book_id = :book"
                        f"  {cutoff_sql}"
                        " AND ("
                        "   (c.world_valid_kind = 'story_time'"
                        "    AND c.world_valid_from <= :chapter"
                        "    AND (c.world_valid_to IS NULL"
                        "         OR c.world_valid_to >= :chapter))"
                        "   OR (c.world_valid_kind = 'chapter_proxy'"
                        "    AND c.world_valid_from <= :chapter)"
                        " )"
                        " ) q"
                        f" WHERE {_current_filter()}"
                        "   AND q.world_valid_kind != 'unknown'"
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

    def org_state_at(
        self,
        canonical_id: str,
        chapter_ordinal: int,
        *,
        knowledge_cutoff: int | None = None,
    ) -> list[dict]:
        """某世界时间点（章节序）势力/成员关系状态（org world 查询，P1）。

        与 world_state_at 同构：org_claims 按 world_valid 区间过滤
        （story_time 用 from/to、chapter_proxy 用 from、unknown 排除），
        并与 knowledge_cutoff 组合（双时间：世界窗口 + 读者披露）。
        作用域 = canonical 自身 + 名下 mention（org 或 member 任一命中）。
        """
        cutoff_sql, cutoff_params = _cutoff_sql(knowledge_cutoff)
        scope_sql, scope_params = self._scope_sql(self.entity_scope(canonical_id))
        params: dict[str, object] = dict(scope_params)
        params.update(cutoff_params)
        params["book"] = self._book_id
        params["chapter"] = chapter_ordinal
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT q.claim_version_id, q.fact_id, q.observed_ordinal,"
                        " q.org_entity_id, q.member_entity_id, q.role, q.action,"
                        " q.world_valid_kind FROM ("
                        " SELECT c.claim_version_id, c.fact_id, c.observed_ordinal,"
                        " c.operation, c.claim_status, c.world_valid_kind,"
                        " o.org_entity_id, o.member_entity_id, o.role, o.action,"
                        " ROW_NUMBER() OVER (PARTITION BY c.fact_id"
                        "   ORDER BY c._rowid DESC) rn"
                        " FROM v_active_claims c"
                        " JOIN org_claims o ON o.claim_version_id = c.claim_version_id"
                        " WHERE (o.org_entity_id " + scope_sql
                        + " OR o.member_entity_id " + scope_sql + ")"
                        " AND c.book_id = :book"
                        f"  {cutoff_sql}"
                        " AND ("
                        "   (c.world_valid_kind = 'story_time'"
                        "    AND c.world_valid_from <= :chapter"
                        "    AND (c.world_valid_to IS NULL"
                        "         OR c.world_valid_to >= :chapter))"
                        "   OR (c.world_valid_kind = 'chapter_proxy'"
                        "    AND c.world_valid_from <= :chapter)"
                        " )"
                        " ) q"
                        f" WHERE {_current_filter()}"
                        "   AND q.world_valid_kind != 'unknown'"
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

    def world_events_at(
        self, chapter_ordinal: int, *, canonical_id: str | None = None
    ) -> list[dict]:
        """某世界时间点（章节序）发生/可见的事件。

        chapter_proxy：事件在 observed_ordinal 章发生
        （world_valid = [ordinal, ordinal]）；chapter_ordinal 处的事件
        即 observed_ordinal == chapter_ordinal 的 supported 事件。
        canonical_id 可选：限定参与者为该实体的事件（经 event_participants
        匹配实体作用域）。
        """
        scope_sql = ""
        params: dict[str, object] = {"book": self._book_id, "chapter": chapter_ordinal}
        if canonical_id is not None:
            in_sql, scope_params = self._scope_sql(
                self.entity_scope(canonical_id), prefix="p"
            )
            scope_sql = f"AND ep.entity_id {in_sql}"
            params.update(scope_params)
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT DISTINCT c.claim_version_id, e.event_type, e.summary,"
                        " e.sequence_in_chapter, c.observed_ordinal"
                        " FROM event_claims e"
                        " JOIN claims c ON c.claim_version_id = e.claim_version_id"
                        " JOIN claim_observations o ON o.claim_version_id = c.claim_version_id"
                        " JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                        " JOIN chapters ch ON c.observed_chapter_id = ch.chapter_id"
                        " LEFT JOIN event_participants ep"
                        "   ON ep.event_claim_version_id = c.claim_version_id"
                        " WHERE r.status = 'active' AND r.book_id = :book"
                        "   AND c.claim_status = 'supported'"
                        "   AND c.operation != 'retract'"
                        "   AND c.observed_ordinal = :chapter"
                        "   AND EXISTS (SELECT 1 FROM event_participants ep2"
                        "     WHERE ep2.event_claim_version_id = c.claim_version_id)"
                        f"   {scope_sql}"
                        " ORDER BY e.sequence_in_chapter"
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
            d["participants"] = self._event_participant_ids(d["claim_version_id"])
            out.append(d)
        return out

    def _event_participant_ids(self, event_claim_version_id: str) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT entity_id FROM event_participants"
                    " WHERE event_claim_version_id = :v"
                ),
                {"v": event_claim_version_id},
            ).fetchall()
        return [r[0] for r in rows]
