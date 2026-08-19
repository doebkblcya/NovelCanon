"""章节 checkpoint（阶段 04，docs/implementation/04 §2）。

唯一键严格采用：book_id, chapter_id, content_hash, pipeline_version,
prompt_version, compression_version, schema_version。
任一字段变化即失效；命中复用必须记录来源 run；复用不绕过当前 run 验证。
"""

from __future__ import annotations

import json

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from novelcanon.config.hash import stable_config_hash
from novelcanon.storage.repository import now_iso

CHECKPOINT_FIELDS = (
    "book_id",
    "chapter_id",
    "content_hash",
    "pipeline_version",
    "prompt_version",
    "compression_version",
    "schema_version",
)


def checkpoint_key(fields: dict[str, object]) -> str:
    """checkpoint 唯一键：规范化 hash，字段顺序无关。"""
    return stable_config_hash({k: fields[k] for k in CHECKPOINT_FIELDS})


class CheckpointService:
    """章节级 checkpoint 存取（幂等：同键不重复写入）。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_done(self, fields: dict[str, object]) -> dict | None:
        """完全匹配的已完成 checkpoint（含 payload 与来源 run）。"""
        key = checkpoint_key(fields)
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT * FROM run_checkpoints WHERE checkpoint_key = :k"
                        " AND status = 'done'"
                    ),
                    {"k": key},
                )
                .mappings()
                .fetchone()
            )
            return dict(row) if row else None

    def save(
        self,
        run_id: str,
        fields: dict[str, object],
        payload: dict,
        *,
        status: str = "done",
        source_run_id: str | None = None,
    ) -> bool:
        """写入 checkpoint（INSERT OR IGNORE：重复投递幂等）。返回是否新增。"""
        with self._engine.begin() as conn:
            return self.save_with(
                conn, run_id, fields, payload, status=status, source_run_id=source_run_id
            )

    def save_with(
        self,
        conn: Connection,
        run_id: str,
        fields: dict[str, object],
        payload: dict,
        *,
        status: str = "done",
        source_run_id: str | None = None,
    ) -> bool:
        """批量事务版本：复用调用方连接，与 ledger 同事务（writer 批量写）。"""
        result = conn.execute(
            text(
                "INSERT OR IGNORE INTO run_checkpoints (run_id, book_id, chapter_id,"
                " checkpoint_key, content_hash, pipeline_version, prompt_version,"
                " compression_version, schema_version, status, payload, source_run_id,"
                " created_at)"
                " VALUES (:run, :book, :ch, :key, :hash, :pv, :pp, :cv, :sv, :st,"
                " :payload, :src, :ts)"
            ),
            {
                "run": run_id,
                "book": fields["book_id"],
                "ch": fields["chapter_id"],
                "key": checkpoint_key(fields),
                "hash": fields["content_hash"],
                "pv": fields["pipeline_version"],
                "pp": fields["prompt_version"],
                "cv": fields["compression_version"],
                "sv": fields["schema_version"],
                "st": status,
                "payload": json.dumps(payload, ensure_ascii=False),
                "src": source_run_id,
                "ts": now_iso(),
            },
        )
        return result.rowcount == 1

    def done_count(self, run_id: str) -> int:
        with self._engine.connect() as conn:
            return (
                conn.execute(
                    text(
                        "SELECT count(*) FROM run_checkpoints WHERE run_id = :r AND status = 'done'"
                    ),
                    {"r": run_id},
                ).scalar()
                or 0
            )

    def failed_chapters(self, run_id: str) -> list[dict]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT chapter_id, payload FROM run_checkpoints"
                        " WHERE run_id = :r AND status = 'failed'"
                    ),
                    {"r": run_id},
                )
                .mappings()
                .fetchall()
            )
            return [dict(r) for r in rows]
