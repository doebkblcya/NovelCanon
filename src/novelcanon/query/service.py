"""结构化查询（阶段 05 最小闭环；阶段 10 扩展）。

只读 active run 的数据；knowledge_cutoff_chapter 过滤 observed 时间；
所有回答返回 evidence 与章节定位（chapter_id/ordinal/source span）。
"""

from __future__ import annotations

from sqlalchemy import Engine, text


def _cutoff_sql(cutoff: int | None) -> tuple[str, dict[str, object]]:
    if cutoff is None:
        return "", {}
    return "AND c.observed_ordinal <= :cutoff", {"cutoff": cutoff}


class QueryService:
    """基于 active 视图的结构化查询。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def display_name(self, canonical_id: str, *, knowledge_cutoff: int | None = None) -> str | None:
        """某章截止前已披露的展示名（§9.1：只能来自截止前的 alias claim）。"""
        cutoff_sql = ""
        params: dict[str, object] = {}
        if knowledge_cutoff is not None:
            cutoff_sql = "AND a.observed_ordinal <= :cutoff"
            params["cutoff"] = knowledge_cutoff
        params["cid"] = canonical_id
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT a.surface_name FROM entity_alias_claims a"
                    " JOIN extraction_runs r ON a.created_by_run_id = r.run_id"
                    " WHERE a.canonical_id = :cid AND r.status = 'active'"
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
        """实体全部状态字段的当前版本（每 fact 最新）+ 证据。"""
        cutoff_sql, params = _cutoff_sql(knowledge_cutoff)
        params["cid"] = canonical_id
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT q.claim_version_id, q.fact_id, q.field, q.value, q.raw_value,"
                        " q.observed_ordinal"
                        " FROM ("
                        "  SELECT c.claim_version_id, c.fact_id, c.observed_ordinal, s.field,"
                        "         s.value, s.raw_value,"
                        "         ROW_NUMBER() OVER (PARTITION BY c.fact_id"
                        "           ORDER BY c._rowid DESC) rn"
                        "  FROM v_active_claims c"
                        "  JOIN state_claims s ON s.claim_version_id = c.claim_version_id"
                        "  WHERE s.subject_entity_id = :cid"
                        f"  {cutoff_sql}"
                        ") q"
                        " WHERE q.rn = 1"
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
        """一跳关系（from/to 任一为该实体）+ 证据（active）。"""
        cutoff_sql, params = _cutoff_sql(knowledge_cutoff)
        params["cid"] = canonical_id
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT c.claim_version_id, c.fact_id, c.observed_ordinal,"
                        " r.from_entity_id, r.to_entity_id, r.relation_type, r.relation_raw"
                        " FROM v_active_claims c"
                        " JOIN relation_claims r ON r.claim_version_id = c.claim_version_id"
                        " WHERE (r.from_entity_id = :cid OR r.to_entity_id = :cid)"
                        f" {cutoff_sql}"
                        " ORDER BY c.observed_ordinal"
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

    def event_participants(self, event_claim_version_id: str) -> list[dict]:
        """事件参与者（event_participants 关联表，§5.2）。"""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT p.entity_id, p.role, e.canonical_name FROM event_participants p"
                        " JOIN entities e ON e.canonical_id = p.entity_id"
                        " WHERE p.event_claim_version_id = :e"
                    ),
                    {"e": event_claim_version_id},
                )
                .mappings()
                .fetchall()
            )
        return [dict(r) for r in rows]

    def claim_history(self, fact_id: str) -> list[dict]:
        """某 fact 的完整版本历史（append-only，按写入序）。"""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT claim_version_id, operation, supersedes_version_id, claim_status,"
                        " observed_ordinal, created_by_run_id FROM claims"
                        " WHERE fact_id = :f ORDER BY rowid"
                    ),
                    {"f": fact_id},
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
                        " LEFT JOIN claim_evidence e ON e.claim_version_id = c.claim_version_id"
                        " WHERE c.claim_version_id = :v LIMIT 1"
                    ),
                    {"v": claim_version_id_value},
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
