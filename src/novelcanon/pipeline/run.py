"""Run 状态机（阶段 04，docs/implementation/04）。

created → running → validating → ready_to_activate → active；
running/validating → failed / retrying。
状态转换必须通过 CAS 在事务中完成；active 是查询可见性的唯一入口。
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from novelcanon.schemas.ids import SCHEMA_VERSION, new_uuid_id
from novelcanon.schemas.types import RunStatus
from novelcanon.storage.repository import now_iso

_TERMINAL = {RunStatus.ACTIVE, RunStatus.FAILED, RunStatus.SUPERSEDED}


class RunManager:
    """extraction run 生命周期管理（事务 + CAS）。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        book_id: str,
        *,
        input_hash: str = "",
        pipeline_version: str = "",
        prompt_version: str = "",
        compression_version: str = "",
        schema_version: str = SCHEMA_VERSION,
        generation_profile_id: str | None = None,
        embedding_profile_id: str | None = None,
        config_hash: str | None = None,
    ) -> str:
        """创建 run（status=created），保存完整配置快照（04 配置快照）。"""
        run_id = new_uuid_id("run")
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO extraction_runs (run_id, book_id, status, input_hash,"
                    " pipeline_version, prompt_version, schema_version, compression_version,"
                    " generation_profile_id, embedding_profile_id, config_hash, started_at)"
                    " VALUES (:id, :book, 'created', :ih, :pv, :pp, :sv, :cv, :gp, :ep, :cfg, :ts)"
                ),
                {
                    "id": run_id,
                    "book": book_id,
                    "ih": input_hash,
                    "pv": pipeline_version,
                    "pp": prompt_version,
                    "sv": schema_version,
                    "cv": compression_version,
                    "gp": generation_profile_id,
                    "ep": embedding_profile_id,
                    "cfg": config_hash,
                    "ts": now_iso(),
                },
            )
        return run_id

    def transition(self, run_id: str, from_status: RunStatus, to_status: RunStatus) -> bool:
        """CAS 状态转换；非法转换返回 False，不做任何变更。"""
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE extraction_runs SET status = :to,"
                    " finished_at = CASE WHEN :to IN ('active','failed','superseded')"
                    "                    THEN :ts ELSE finished_at END"
                    " WHERE run_id = :id AND status = :from"
                ),
                {
                    "to": to_status.value,
                    "from": from_status.value,
                    "id": run_id,
                    "ts": now_iso(),
                },
            )
            return result.rowcount == 1

    def fail(self, run_id: str, error: str) -> None:
        """从可失败状态进入 failed（running/created/retrying/validating）。"""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE extraction_runs SET status = 'failed', finished_at = :ts, error = :e"
                    " WHERE run_id = :id AND status IN"
                    " ('created','running','retrying','validating')"
                ),
                {"id": run_id, "ts": now_iso(), "e": error},
            )

    def get(self, run_id: str) -> dict | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT * FROM extraction_runs WHERE run_id = :id"), {"id": run_id}
                )
                .mappings()
                .fetchone()
            )
            return dict(row) if row else None

    def is_terminal(self, run_id: str) -> bool:
        run = self.get(run_id)
        return run is not None and run["status"] in {s.value for s in _TERMINAL}
