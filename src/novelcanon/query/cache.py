"""查询缓存（阶段 10 §5，docs/implementation/10）。

缓存键至少包含（10 §5）：
- book_id；
- 标准化查询（router 归一化后）；
- query type；
- knowledge cutoff；
- world at chapter；
- active run / index version（签名）；
- query / synthesis profile。

active run 或依赖版本改变后，签名变化 → 键变化 → 旧缓存不再命中
（10 §5「active run 或依赖版本改变后，旧缓存不能继续返回」）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import Engine, text

from novelcanon.config.hash import stable_config_hash
from novelcanon.storage.repository import now_iso

CACHE_POLICY_VERSION = "cache-v1"


def active_state_signature(engine: Engine, book_id: str) -> str:
    """active run 集合 + active index version 的签名。

    任一 active run 变化（新增/换人/失效）或索引版本切换 → 签名变化
    → 旧缓存键失效。签名只依赖不可变身份字段（run_id/status、
    index_version_id/status），不依赖变更时间戳，保证同状态幂等。
    """
    with engine.connect() as conn:
        runs = conn.execute(
            text(
                "SELECT run_id, status FROM extraction_runs"
                " WHERE book_id = :b AND status = 'active' ORDER BY run_id"
            ),
            {"b": book_id},
        ).fetchall()
        index = conn.execute(
            text(
                "SELECT index_version_id, status FROM index_versions"
                " WHERE book_id = :b AND status = 'active' ORDER BY index_version_id"
            ),
            {"b": book_id},
        ).fetchall()
    return stable_config_hash(
        {
            "active_runs": [[r[0], r[1]] for r in runs],
            "active_indexes": [[r[0], r[1]] for r in index],
        }
    )


@dataclass(frozen=True)
class CacheKey:
    """版本化缓存键的输入（hash 后即为 query_cache.cache_key）。"""

    book_id: str
    normalized_query: str
    query_type: str
    knowledge_cutoff: int | None
    world_at: int | None
    active_run_signature: str
    index_version_id: str | None
    query_profile: str
    synthesis_profile: str | None

    def to_key(self) -> str:
        return stable_config_hash(
            {
                "policy": CACHE_POLICY_VERSION,
                "book_id": self.book_id,
                "query": self.normalized_query,
                "type": self.query_type,
                "cutoff": self.knowledge_cutoff,
                "world": self.world_at,
                "active": self.active_run_signature,
                "index": self.index_version_id,
                "qprofile": self.query_profile,
                "sprofile": self.synthesis_profile,
            }
        )


class QueryCache:
    """query_cache 表的读写（book_id 绑定，多书隔离）。"""

    def __init__(self, engine: Engine, book_id: str) -> None:
        self._engine = engine
        self._book_id = book_id

    def get(self, key: CacheKey) -> dict | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT result FROM query_cache"
                        " WHERE cache_key = :k AND book_id = :b"
                    ),
                    {"k": key.to_key(), "b": self._book_id},
                )
                .fetchone()
            )
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def put(self, key: CacheKey, result: dict) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO query_cache (cache_key, book_id,"
                    " normalized_query, query_type, knowledge_cutoff, world_at,"
                    " active_run_signature, index_version_id, query_profile,"
                    " synthesis_profile, result, created_at)"
                    " VALUES (:k, :b, :q, :t, :cutoff, :world, :sig, :idx,"
                    " :qp, :sp, :result, :ts)"
                ),
                {
                    "k": key.to_key(),
                    "b": self._book_id,
                    "q": key.normalized_query,
                    "t": key.query_type,
                    "cutoff": key.knowledge_cutoff,
                    "world": key.world_at,
                    "sig": key.active_run_signature,
                    "idx": key.index_version_id,
                    "qp": key.query_profile,
                    "sp": key.synthesis_profile,
                    "result": json.dumps(result, ensure_ascii=False),
                    "ts": now_iso(),
                },
            )
