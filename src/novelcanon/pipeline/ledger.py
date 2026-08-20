"""Token 账本（阶段 04，docs/implementation/04 §5）。

每次模型调用记录 input/cached/reasoning/output/retry/discarded tokens，
以及 provider/model/profile 和 book/run/chapter/stage 定位；
统计可汇总到章节、阶段、run 与整本书。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from novelcanon.storage.repository import now_iso


@dataclass(frozen=True)
class Usage:
    """一次模型调用的 token 计量。"""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    output_tokens: int = 0
    retry_count: int = 0
    discarded_tokens: int = 0
    provider: str | None = None
    model: str | None = None
    profile_id: str | None = None

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            retry_count=self.retry_count + other.retry_count,
            discarded_tokens=self.discarded_tokens + other.discarded_tokens,
            provider=self.provider or other.provider,
            model=self.model or other.model,
            profile_id=self.profile_id or other.profile_id,
        )

    def total(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.reasoning_tokens
            + self.output_tokens
            + self.discarded_tokens
        )


@dataclass(frozen=True)
class LedgerEntry:
    run_id: str | None  # 查询/摘要记账无 run 归属（0015 run_id 可空）
    book_id: str
    chapter_id: str | None
    stage: str
    usage: Usage = field(default_factory=Usage)


class TokenLedger:
    """token_ledger 写入与汇总。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, entry: LedgerEntry) -> None:
        with self._engine.begin() as conn:
            self.record_with(conn, entry)

    def record_with(self, conn: Connection, entry: LedgerEntry) -> None:
        """批量事务版本：复用调用方连接（writer 批量写）。"""
        u = entry.usage
        conn.execute(
            text(
                "INSERT INTO token_ledger (run_id, book_id, chapter_id, stage,"
                " provider, model, profile_id, input_tokens, cached_input_tokens,"
                " reasoning_tokens, output_tokens, retry_count, discarded_tokens,"
                " created_at)"
                " VALUES (:run, :book, :ch, :stage, :prov, :model, :prof,"
                " :inp, :cached, :reason, :out, :retry, :disc, :ts)"
            ),
            {
                "run": entry.run_id,
                "book": entry.book_id,
                "ch": entry.chapter_id,
                "stage": entry.stage,
                "prov": u.provider,
                "model": u.model,
                "prof": u.profile_id,
                "inp": u.input_tokens,
                "cached": u.cached_input_tokens,
                "reason": u.reasoning_tokens,
                "out": u.output_tokens,
                "retry": u.retry_count,
                "disc": u.discarded_tokens,
                "ts": now_iso(),
            },
        )

    def summary(self, run_id: str) -> dict[str, int]:
        """按 run 汇总 token（04 验证：汇总与模拟 provider 返回值一致）。"""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,"
                        " COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,"
                        " COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,"
                        " COALESCE(SUM(output_tokens),0) AS output_tokens,"
                        " COALESCE(SUM(retry_count),0) AS retry_count,"
                        " COALESCE(SUM(discarded_tokens),0) AS discarded_tokens,"
                        " COALESCE(SUM(input_tokens + cached_input_tokens + reasoning_tokens"
                        " + output_tokens + discarded_tokens),0) AS total"
                        " FROM token_ledger WHERE run_id = :r"
                    ),
                    {"r": run_id},
                )
                .mappings()
                .fetchone()
            )
        return dict(row) if row else {}

    def by_stage(self, run_id: str) -> dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT stage, COALESCE(SUM(input_tokens + cached_input_tokens"
                    " + reasoning_tokens + output_tokens + discarded_tokens),0) AS total"
                    " FROM token_ledger WHERE run_id = :r GROUP BY stage"
                ),
                {"r": run_id},
            ).fetchall()
        return {r[0]: r[1] for r in rows}
