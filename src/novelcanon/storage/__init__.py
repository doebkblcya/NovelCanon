"""存储层（ADR-0002）：SQLAlchemy Core（同步）+ SQLite、repository、迁移。

- 单 writer、WAL、busy_timeout、foreign_keys=ON；
- migration 全部手写 revision（FTS/vec 虚表、trigger、表重建均不依赖 autogenerate）。
"""

from novelcanon.storage.engine import BUSY_TIMEOUT_MS, create_db_engine
from novelcanon.storage.evidence_policy import aggregate_claim_status
from novelcanon.storage.repository import Repository, WriteResult, now_iso

__all__ = [
    "BUSY_TIMEOUT_MS",
    "Repository",
    "WriteResult",
    "aggregate_claim_status",
    "create_db_engine",
    "now_iso",
]
