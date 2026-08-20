"""Map 产物 staging（阶段 06，docs/implementation/06 产物）。

每章 Map 结果（合法 Draft 或校验失败记录）写入 map_drafts 表：
- UNIQUE(run_id, chapter_id)：同 run 重复投递幂等（INSERT OR IGNORE）；
- draft_id 确定性：book/chapter/内容/版本（run 不进 ID，跨 run 稳定）；
- status: valid（合法 Draft）/ invalid（有响应但不合规）/ failed（无响应）；
- 无效输出保存错误摘要、响应 hash 与结构化 validation_issues
  （完整原始响应按安全配置存 raw_response，不进入正式事实表）。

staging 由 runner 的 writer 在批量事务中写入（single writer 原则）。
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from novelcanon.config.hash import stable_config_hash
from novelcanon.pipeline.runner import ChapterTask
from novelcanon.storage.repository import now_iso


def draft_id(
    book_id: str,
    chapter_id: str,
    content_hash: str,
    prompt_version: str,
    schema_version: str,
) -> str:
    """确定性 draft_id：run 不进 ID（同配置重跑得到相同 ID）。"""
    return (
        "draft_"
        + stable_config_hash(
            {
                "book_id": book_id,
                "chapter_id": chapter_id,
                "content_hash": content_hash,
                "prompt_version": prompt_version,
                "schema_version": schema_version,
            }
        )[:16]
    )


class MapStaging:
    """map_drafts 写入（writer 事务内调用；同 run 同章幂等）。"""

    def __init__(self, conn: Connection | None = None) -> None:
        self._conn = conn

    def write(
        self,
        conn: Connection,
        run_id: str,
        task: ChapterTask,
        payload: dict,
        *,
        source_run_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """写一行 staging；payload 由 map process_fn 产出（含 status/draft/issues）。"""
        fields = task.checkpoint_fields
        status = payload.get("status") or ("failed" if error else "valid")
        issues = payload.get("validation_issues", [])
        draft = payload.get("draft")
        conn.execute(
            text(
                "INSERT OR IGNORE INTO map_drafts (draft_id, run_id, book_id, chapter_id,"
                " ordinal, content_hash, prompt_version, schema_version, status,"
                " draft_json, request_hash, response_hash, validation_issues,"
                " error_summary, raw_response, created_at)"
                " VALUES (:did, :run, :book, :ch, :ord, :hash, :pv, :sv, :st,"
                " :draft, :req, :resp, :issues, :err, :raw, :ts)"
            ),
            {
                "did": draft_id(
                    str(fields["book_id"]),
                    str(fields["chapter_id"]),
                    str(fields["content_hash"]),
                    str(fields.get("prompt_version", "")),
                    str(fields.get("schema_version", "")),
                ),
                "run": run_id,
                "book": fields["book_id"],
                "ch": fields["chapter_id"],
                "ord": task.ordinal,
                "hash": fields["content_hash"],
                "pv": fields.get("prompt_version", ""),
                "sv": fields.get("schema_version", ""),
                "st": status,
                "draft": json.dumps(draft, ensure_ascii=False) if draft else None,
                "req": payload.get("request_hash", ""),
                "resp": payload.get("response_hash", ""),
                "issues": json.dumps(issues, ensure_ascii=False),
                "err": error or payload.get("error_summary"),
                "raw": payload.get("raw_response"),
                "ts": now_iso(),
            },
        )
