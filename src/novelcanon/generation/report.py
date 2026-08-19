"""抽取报告（阶段 06，docs/implementation/06 产物）。

按 run 从 staging（map_drafts）与 token_ledger 汇总：
- 章节状态分布（valid / invalid / failed）；
- 抽取量（mentions / unresolved / local_events / claims 总数与按类型）；
- 校验问题按 code 统计（系统性错误定位，06 §5 质量迭代）；
- token 汇总（复用 TokenLedger.summary）。
"""

from __future__ import annotations

import json

from sqlalchemy import Engine, text


def extraction_report(engine: Engine, run_id: str) -> dict:
    """返回 run 的抽取报告（纯查询，不修改数据）。"""
    with engine.connect() as conn:
        chapters = conn.execute(
            text(
                "SELECT status, COUNT(*) AS n FROM map_drafts WHERE run_id = :r"
                " GROUP BY status"
            ),
            {"r": run_id},
        ).fetchall()
        status_counts = {row[0]: row[1] for row in chapters}

        issue_rows = conn.execute(
            text(
                "SELECT validation_issues FROM map_drafts"
                " WHERE run_id = :r AND status != 'valid'"
            ),
            {"r": run_id},
        ).fetchall()
        issues_by_code: dict[str, int] = {}
        for (issues_json,) in issue_rows:
            try:
                issues = json.loads(issues_json or "[]")
            except json.JSONDecodeError:
                continue
            for issue in issues:
                code = issue.get("code", "unknown") if isinstance(issue, dict) else "unknown"
                issues_by_code[code] = issues_by_code.get(code, 0) + 1

        count_rows = conn.execute(
            text(
                "SELECT"
                " COALESCE(SUM(CASE WHEN status='valid' THEN 1 ELSE 0 END),0) AS valid,"
                " COUNT(*) AS total,"
                " COALESCE(SUM(json_array_length(json_extract(draft_json,'$.mentions'))),0)"
                "   AS mentions,"
                " COALESCE(SUM(json_array_length(json_extract(draft_json,'$.unresolved'))),0)"
                "   AS unresolved,"
                " COALESCE(SUM(json_array_length(json_extract(draft_json,'$.local_events'))),0)"
                "   AS local_events,"
                " COALESCE(SUM(json_array_length(json_extract(draft_json,"
                "   '$.provisional_claims'))),0) AS claims"
                " FROM map_drafts WHERE run_id = :r"
            ),
            {"r": run_id},
        ).mappings().fetchone()

        claims_by_type: dict[str, int] = {}
        claim_rows = conn.execute(
            text(
                "SELECT draft_json FROM map_drafts WHERE run_id = :r AND status='valid'"
            ),
            {"r": run_id},
        ).fetchall()
        for (draft_json,) in claim_rows:
            if not draft_json:
                continue
            try:
                draft = json.loads(draft_json)
            except json.JSONDecodeError:
                continue
            for claim in draft.get("provisional_claims", []):
                ctype = claim.get("claim_type", "unknown")
                claims_by_type[ctype] = claims_by_type.get(ctype, 0) + 1

    from novelcanon.pipeline.ledger import TokenLedger

    tokens = TokenLedger(engine).summary(run_id)
    counts = dict(count_rows) if count_rows else {}
    return {
        "chapters": {
            "total": counts.get("total", 0),
            "valid": counts.get("valid", 0),
            **status_counts,
        },
        "extraction": {
            "mentions": counts.get("mentions", 0),
            "unresolved": counts.get("unresolved", 0),
            "local_events": counts.get("local_events", 0),
            "claims": counts.get("claims", 0),
            "claims_by_type": claims_by_type,
        },
        "validation_issues_by_code": issues_by_code,
        "tokens": tokens,
    }
