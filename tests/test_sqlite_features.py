"""SQLite 编译特性与 sqlite-vec 扩展加载 smoke（ADR-0002/0006）。

CI 必跑：验证开发机与 CI 环境的 SQLite 特性一致，避免隐性环境依赖。
"""

import sqlite3

import pytest

REQUIRED_SQLITE = (3, 41, 0)


def _version_tuple() -> tuple[int, ...]:
    return tuple(int(part) for part in sqlite3.sqlite_version.split("."))


def test_sqlite_version_meets_minimum() -> None:
    assert _version_tuple() >= REQUIRED_SQLITE


def test_fts5_available() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
        conn.execute("INSERT INTO t VALUES (?)", ("测试文本",))
        row = conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", ("测试文本",)).fetchone()
        assert row is not None and row[0] == 1
    finally:
        conn.close()


def test_json1_available() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        row = conn.execute("SELECT json_extract(?, '$.a')", ('{"a": 1}',)).fetchone()
        assert row is not None and row[0] == 1
    finally:
        conn.close()


def test_sqlite_vec_loadable() -> None:
    sqlite_vec = pytest.importorskip("sqlite_vec", reason="未安装 vec extra")
    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        row = conn.execute("SELECT vec_version()").fetchone()
        assert row is not None and row[0]
    finally:
        conn.close()
