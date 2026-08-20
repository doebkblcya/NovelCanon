"""按验证 run 选择 evidence 的统一入口（阶段 11 复审 P1）。

0017 允许同 claim/span 多验证并存（v1/v2、跨 run）后，所有消费
claim_evidence 的模块必须遵循**同一语义**，否则各处手写不同的 NULL
兼容条件会导致隔离不一致：

exact-current-first：
    当前（active/link）run 的验证记录优先；仅当该 claim+span **不存在**
    当前 run 记录时才回退 legacy NULL（早于 run 机制写入、
    verification_run_id IS NULL 的行）。

禁止手写 `verification_run_id IS NULL OR = :r`：那会让 legacy 与 current
同时返回同一 span 的两套证据（查询重复、chapter_citation LIMIT 1 可能
取到 legacy）。exact-current-first 用 NOT EXISTS 表达——每个 span 维度
独立判断，不做整 claim 一刀切。

消费者：QueryService（_evidence_for / chapter_citation）、
EventLinkService（_evidence_stances / _evidence_ordinals / _source_evidence）、
Validator 激活门禁、Pilot 黄金证据复现（_evidence_hashes）。
"""

from __future__ import annotations

from sqlalchemy import Engine, text


def active_run_id(engine: Engine, book_id: str) -> str | None:
    """book 当前 active run id（无 active 返回 None）。"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT run_id FROM extraction_runs WHERE book_id = :b AND status = 'active'"),
            {"b": book_id},
        ).fetchone()
    return str(row[0]) if row else None


def evidence_run_condition(alias: str = "e", run_param: str = "vr") -> str:
    """exact-current-first 过滤子句（可内嵌 WHERE / LEFT JOIN ... ON）。

    alias：claim_evidence 表在外层查询中的别名（自关联子查询固定引用
    物理表 claim_evidence，别名只修饰外层行引用）。
    run_param：当前 run 的绑定参数名（默认 vr，每查询只用一个 run 参数）。

    返回形如 `(e.verification_run_id = :vr OR (e.verification_run_id IS NULL
    AND NOT EXISTS (...)))` 的子句——当前 run 优先，仅当该 claim+span
    无当前 run 记录时回退 legacy NULL。
    """
    return (
        f"({alias}.verification_run_id = :{run_param}"
        " OR ("
        f" {alias}.verification_run_id IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM claim_evidence e2"
        f"  WHERE e2.claim_version_id = {alias}.claim_version_id"
        f"    AND e2.chapter_id = {alias}.chapter_id"
        f"    AND e2.char_start = {alias}.char_start"
        f"    AND e2.char_end = {alias}.char_end"
        f"    AND e2.span_hash = {alias}.span_hash"
        f"    AND e2.verification_run_id = :{run_param})))"
    )
