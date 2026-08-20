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

        # P1（十五轮）：激活前硬门禁——该 run 的每个 supported claim 必须有
        # 至少一条当前验证版本的 evidence 且 primary_evidence_id 非空真实。
        # 防止 Map checkpoint 复用带入的旧下游 claim（无新 align 证据）泄漏
        # 进 active，违反「所有正式回答可追溯到 supported evidence」。
        # P1（十六轮）：evidence 必须属于**待激活 run**（verification_run_id
        # = :r）——旧 run 验证的 primary 不得通过门禁（0017 允许多 run 并存
        # 后，仅「指向真实 evidence」不足以保证当前 run 完成对齐）。版本
        # 正确性由 run 绑定保证：同一 run 的 align 只用当前 verifier 版本。
        with self._engine.connect() as conn:
            orphans = conn.execute(
                text(
                    "SELECT COUNT(*) FROM claims c"
                    " JOIN claim_observations o ON o.claim_version_id = c.claim_version_id"
                    " WHERE o.extraction_run_id = :r AND c.claim_status = 'supported'"
                    " AND (c.primary_evidence_id IS NULL"
                    "      OR NOT EXISTS (SELECT 1 FROM claim_evidence ce"
                    "                     WHERE ce.claim_version_id = c.claim_version_id"
                    "                       AND ce.evidence_id = c.primary_evidence_id"
                    "                       AND ce.verification_run_id = :r)"
                    "      OR NOT EXISTS (SELECT 1 FROM claim_evidence ce"
                    "                     WHERE ce.claim_version_id = c.claim_version_id"
                    "                       AND ce.verification_run_id = :r))"
                ),
                {"r": run_id},
            ).scalar()
        if orphans:
            issues.append(
                f"supported 但无有效 primary evidence 的 claim：{orphans} 条"
                f"（run {run_id}，primary 须属于本 run 且有本 run 证据）——不得激活"
            )
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
