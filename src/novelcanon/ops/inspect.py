"""库内对象检查（阶段二 07：inspect CLI）。

纯只读诊断：book/run/index/各知识类型计数/完整性异常。输出 dict，
CLI 可渲染为文本或 JSON（机器可读，供脚本与前端状态页复用）。

计数一律按 book 过滤（claims 经 observed_chapter_id → chapters 关联，
实体经 created_by_run_id → extraction_runs 关联）。
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from novelcanon.evidence.selector import evidence_run_condition
from novelcanon.graph.queries import _entity_set_sql


def inspect_book(engine: Engine, book_id: str) -> dict:
    """单书完整检查报告。"""
    with engine.connect() as conn:
        book = (
            conn.execute(
                text(
                    "SELECT book_id, title, source_format, raw_content_hash,"
                    " normalized_content_hash,"
                    " (SELECT COUNT(*) FROM chapters c WHERE c.book_id = books.book_id)"
                    "   AS chapter_count"
                    " FROM books WHERE book_id = :b"
                ),
                {"b": book_id},
            )
            .mappings()
            .fetchone()
        )
        if book is None:
            return {"book_id": book_id, "error": "book_not_found"}

        runs = conn.execute(
            text(
                "SELECT status, COUNT(*) AS n FROM extraction_runs"
                " WHERE book_id = :b GROUP BY status ORDER BY status"
            ),
            {"b": book_id},
        ).all()
        active_run = (
            conn.execute(
                text(
                    "SELECT run_id, pipeline_version, generation_profile_id, embedding_profile_id,"
                    " started_at, finished_at, error"
                    " FROM extraction_runs WHERE book_id = :b AND status = 'active'"
                    " ORDER BY rowid DESC LIMIT 1"
                ),
                {"b": book_id},
            )
            .mappings()
            .fetchone()
        )
        failed_runs = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT run_id, status, error FROM extraction_runs"
                    " WHERE book_id = :b AND status IN ('running', 'failed')"
                    " ORDER BY rowid"
                ),
                {"b": book_id},
            )
            .mappings()
            .fetchall()
        ]
        index = (
            conn.execute(
                text(
                    "SELECT index_version_id, chunking_version, embedding_profile_id, status,"
                    " (SELECT COUNT(*) FROM embedding_records er"
                    "   WHERE er.index_version_id = iv.index_version_id"
                    "     AND er.profile_id = iv.embedding_profile_id) AS record_count"
                    " FROM index_versions iv"
                    " WHERE iv.book_id = :b AND iv.status = 'active'"
                    " ORDER BY rowid DESC LIMIT 1"
                ),
                {"b": book_id},
            )
            .mappings()
            .fetchone()
        )
        counts = _counts(conn, book_id, active_run["run_id"] if active_run else None)
        warnings = _warnings(conn, book_id, counts, active_run, index)

    return {
        "book_id": book_id,
        "title": book["title"],
        "source_format": book["source_format"],
        "raw_content_hash": book["raw_content_hash"],
        "normalized_content_hash": book["normalized_content_hash"],
        "chapter_count": book["chapter_count"],
        "runs": {
            "by_status": {r[0]: r[1] for r in runs},
            "active_run_id": active_run["run_id"] if active_run else None,
            "active_run_pipeline": active_run["pipeline_version"] if active_run else None,
            "failed": failed_runs,
        },
        "index": {
            "index_version_id": index["index_version_id"] if index else None,
            "embedding_profile_id": index["embedding_profile_id"] if index else None,
            "record_count": index["record_count"] if index else 0,
            "status": index["status"] if index else None,
        },
        "counts": counts,
        "warnings": warnings,
    }


def inspect_all(engine: Engine) -> dict:
    """全部书摘要（inspect 无 --book 时的总览）。"""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT b.book_id, b.title,"
                    " (SELECT COUNT(*) FROM chapters c WHERE c.book_id = b.book_id)"
                    "   AS chapter_count,"
                    " (SELECT COUNT(*) FROM extraction_runs r"
                    "   WHERE r.book_id = b.book_id AND r.status = 'active') AS active_runs,"
                    " (SELECT COUNT(*) FROM index_versions iv"
                    "   WHERE iv.book_id = b.book_id AND iv.status = 'active') AS active_indexes,"
                    " (SELECT COUNT(*) FROM v_active_claims c"
                    "   WHERE c.book_id = b.book_id) AS active_claims"
                    " FROM books b ORDER BY b.created_at"
                )
            )
            .mappings()
            .fetchall()
        )
    books = [dict(r) for r in rows]
    return {
        "book_count": len(books),
        "books": books,
    }


def _counts(conn, book_id: str, active_run_id: str | None) -> dict:
    """计数：active（当前激活 run 可见的数据）与 history（全量含历史）。

    复审 P1：历史 run 的 claim/evidence/实体不应混入「当前」统计——
    完整性判断与展示以 active 为准，历史只作审计参考。口径：
    - evidence：exact-current-first（复用 evidence.selector）——同 span
      已有当前 run 验证行时，legacy NULL 行不计入（不得 552=276+276）；
    - event_links：按 active run 的 event_link_verifications 统计（当前
      run 验证记录数 + supported 数），不再错误地经 v_active_claims 关联；
    - entities：与实体目录同源（_entity_set_sql 投影后 canonical）——
      产品目录去重数（103），不是 mention/canonical 混合的 586。
    """

    def one(sql: str, params: dict | None = None) -> int:
        return conn.execute(text(sql), params or {"b": book_id}).scalar_one()

    active = {
        "claims": one("SELECT COUNT(*) FROM v_active_claims WHERE book_id = :b"),
        "evidence": one(
            "SELECT COUNT(*) FROM claim_evidence e"
            " JOIN v_active_claims c ON c.claim_version_id = e.claim_version_id"
            f" WHERE c.book_id = :b AND {evidence_run_condition()}",
            {"b": book_id, "vr": active_run_id},
        ),
        "entities": one(
            f"SELECT COUNT(*) FROM ({_entity_set_sql(cutoff=False)}) s",
            {"b": book_id},
        ),
        "entity_aliases": one(
            "SELECT COUNT(*) FROM entity_alias_claims a"
            " JOIN alias_observations o ON o.claim_version_id = a.claim_version_id"
            " JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
            " WHERE r.status = 'active' AND r.book_id = :b"
        ),
        "events": one(
            "SELECT COUNT(*) FROM event_claims e"
            " JOIN v_active_claims c ON c.claim_version_id = e.claim_version_id"
            " WHERE c.book_id = :b"
        ),
        # 当前 run 的因果边验证记录（事件链接数）+ supported 数（复审 P1：
        # event_links 不是 claims 表事实，不能经 v_active_claims 关联）
        "event_links": one(
            "SELECT COUNT(DISTINCT v.claim_version_id) FROM event_link_verifications v"
            " JOIN extraction_runs r ON r.run_id = v.extraction_run_id"
            " WHERE r.status = 'active' AND r.book_id = :b"
        ),
        "event_links_supported": one(
            "SELECT COUNT(DISTINCT v.claim_version_id) FROM event_link_verifications v"
            " JOIN extraction_runs r ON r.run_id = v.extraction_run_id"
            " WHERE r.status = 'active' AND r.book_id = :b"
            "   AND v.claim_status = 'supported'"
        ),
    }
    history = {
        "claims": one(
            "SELECT COUNT(*) FROM claims c"
            " JOIN chapters ch ON ch.chapter_id = c.observed_chapter_id"
            " WHERE ch.book_id = :b"
        ),
        "evidence": one(
            "SELECT COUNT(*) FROM claim_evidence e"
            " JOIN claims c ON c.claim_version_id = e.claim_version_id"
            " JOIN chapters ch ON ch.chapter_id = c.observed_chapter_id"
            " WHERE ch.book_id = :b"
        ),
        "entities": one(
            "SELECT COUNT(*) FROM entities e"
            " JOIN extraction_runs r ON r.run_id = e.created_by_run_id"
            " WHERE r.book_id = :b"
        ),
        "entity_aliases": one(
            "SELECT COUNT(*) FROM entity_alias_claims a"
            " JOIN alias_observations o ON o.claim_version_id = a.claim_version_id"
            " JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
            " WHERE r.book_id = :b"
        ),
        "mentions": one(
            "SELECT COUNT(*) FROM entity_mentions m"
            " JOIN extraction_runs r ON r.run_id = m.run_id WHERE r.book_id = :b"
        ),
        "events": one(
            "SELECT COUNT(*) FROM event_claims e"
            " JOIN claims c ON c.claim_version_id = e.claim_version_id"
            " JOIN chapters ch ON ch.chapter_id = c.observed_chapter_id"
            " WHERE ch.book_id = :b"
        ),
        "event_links": one(
            "SELECT COUNT(*) FROM event_links l"
            " JOIN chapters ch ON ch.chapter_id = l.observed_chapter_id"
            " WHERE ch.book_id = :b"
        ),
        "summaries": one("SELECT COUNT(*) FROM summary_artifacts WHERE book_id = :b"),
    }
    return {"active": active, "history": history}


def _warnings(conn, book_id: str, counts: dict, active_run, index) -> list[str]:
    warnings: list[str] = []
    active = counts["active"]
    if active["claims"] == 0:
        warnings.append("无 active claim（当前激活 run 无事实）")
    if active_run is None:
        warnings.append("无 active run（数据未激活，查询默认不可见）")
    if index is None:
        warnings.append("无 active 索引（向量检索不可用）")
    elif index["record_count"] == 0:
        warnings.append("active 索引记录数为 0（索引为空）")
    # 完整性警告只检查 active run 的 claim 无直接证据（复审 P1：历史
    # claim 的 orphan 不计入——非 active 数据不参与当前查询语义）
    orphan = conn.execute(
        text(
            "SELECT COUNT(*) FROM v_active_claims c"
            " WHERE c.book_id = :b AND c.primary_evidence_id IS NULL"
        ),
        {"b": book_id},
    ).scalar_one()
    if orphan:
        warnings.append(f"{orphan} 条 active claim 无 primary_evidence（证据链不完整）")
    return warnings
