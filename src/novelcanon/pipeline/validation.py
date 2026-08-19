"""验证与激活（阶段 04，docs/implementation/04 §6）。

激活前验证：章节任务全部完成、Schema/外键完整、错误比例在阈值内；
激活操作瞬时且有界；失败时旧 active run 保持可查。
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from novelcanon.pipeline.checkpoint import CheckpointService
from novelcanon.schemas.types import RunStatus
from novelcanon.storage.repository import now_iso


class Validator:
    """激活前验证；issues() 返回问题列表（空 = 通过）。"""

    def __init__(self, engine: Engine, error_ratio_threshold: float = 0.0) -> None:
        self._engine = engine
        self._threshold = error_ratio_threshold

    def issues(self, run_id: str, *, total_chapters: int) -> list[str]:
        issues: list[str] = []
        checkpoint = CheckpointService(self._engine)
        done = checkpoint.done_count(run_id)
        if done < total_chapters:
            issues.append(f"章节任务未完成：{done}/{total_chapters}")

        failed = checkpoint.failed_chapters(run_id)
        if failed and self._threshold == 0.0:
            issues.append(f"存在失败章节（无豁免）：{[f['chapter_id'] for f in failed[:5]]}")
        elif total_chapters > 0 and len(failed) / total_chapters > self._threshold:
            issues.append(f"失败比例 {len(failed)}/{total_chapters} 超过阈值 {self._threshold}")

        with self._engine.connect() as conn:
            fk = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
        if fk:
            issues.append(f"外键完整性检查失败：{len(fk)} 处")
        return issues


class Activator:
    """原子激活：ready_to_activate → active，同书旧 active → superseded。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def activate(self, run_id: str) -> list[str] | None:
        with self._engine.begin() as conn:
            run = (
                conn.execute(
                    text("SELECT book_id, status FROM extraction_runs WHERE run_id = :id"),
                    {"id": run_id},
                )
                .mappings()
                .fetchone()
            )
            if run is None:
                return [f"run {run_id} 不存在"]
            if run["status"] != RunStatus.READY_TO_ACTIVATE.value:
                return [f"run 状态为 {run['status']}，期望 ready_to_activate"]

            # 同一事务：旧 active → superseded；新 run → active
            conn.execute(
                text(
                    "UPDATE extraction_runs SET status = 'superseded', finished_at = :ts"
                    " WHERE book_id = :b AND status = 'active'"
                ),
                {"b": run["book_id"], "ts": now_iso()},
            )
            conn.execute(
                text(
                    "UPDATE extraction_runs SET status = 'active', finished_at = :ts"
                    " WHERE run_id = :id"
                ),
                {"id": run_id, "ts": now_iso()},
            )
        return None
