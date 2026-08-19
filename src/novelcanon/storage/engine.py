"""SQLite 连接工厂（ADR-0002）。

- WAL、busy_timeout、foreign_keys=ON 在每次连接建立时统一设置；
- sqlite-vec 扩展经 connect event 加载，加载后立即关闭 extension loading；
- 写入端由 single writer 串行化（阶段 04 实现 writer service）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

BUSY_TIMEOUT_MS = 5000


def create_db_engine(db_path: Path, *, enable_vec: bool = False) -> Engine:
    """创建同步 SQLAlchemy engine。

    ``enable_vec=True`` 时在每次连接上加载 sqlite-vec 扩展（ADR-0006），
    并校验 sqlite_version() >= 3.41 与 vec_version()。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": BUSY_TIMEOUT_MS / 1000, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: sqlite3.Connection, _record: object) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        if enable_vec:
            _load_vec_extension(dbapi_conn)

    return engine


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    import sqlite_vec  # type: ignore[import-untyped]

    version = tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    if version < (3, 41, 0):
        raise RuntimeError(f"sqlite-vec 需要 SQLite >= 3.41，当前 {sqlite3.sqlite_version}")
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    vec_version = conn.execute("SELECT vec_version()").fetchone()
    if not vec_version:
        raise RuntimeError("sqlite-vec 加载后 vec_version() 为空")
