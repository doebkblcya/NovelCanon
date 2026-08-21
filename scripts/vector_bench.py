#!/usr/bin/env python3
"""生产向量检索压测（阶段二 07：生产向量检索性能）。

对比 BruteForceVectorStore（全扫描）与 SqliteVecVectorStore（vec0 KNN）：
- 数据：真实库 book 的 embedding_records（如百年孤独 909 chunks / 1024 维）；
- 指标：重建耗时、单查询 p50/p95 延迟、索引体积；
- 运行：.venv/bin/python scripts/vector_bench.py [--book book_cc] [--queries 30] [--top-k 8]
- 结论：短篇规模下是否维持暴力扫描，还是切换 sqlite-vec（07 流程决策输入）。
"""

from __future__ import annotations

import argparse
import random
import struct
import tempfile
import time
from pathlib import Path

from sqlalchemy import create_engine, text

from novelcanon.retrieval.vectorstore import BruteForceVectorStore, SqliteVecVectorStore


def load_records(db_path: Path, book_id: str) -> tuple[list[tuple[int, str, bytes]], str, int]:
    """读真实库 embedding_records + active index_version_id。"""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        iv_row = conn.execute(
            text(
                "SELECT index_version_id FROM index_versions"
                " WHERE book_id = :b AND status = 'active' ORDER BY rowid DESC LIMIT 1"
            ),
            {"b": book_id},
        ).fetchone()
        if iv_row is None:
            engine.dispose()
            raise SystemExit(f"❌ {book_id} 无 active 索引")
        iv = iv_row[0]
        rows = conn.execute(
            text(
                "SELECT record_id, raw_chunk_id, vector FROM embedding_records"
                " WHERE book_id = :b AND index_version_id = :iv AND vector IS NOT NULL"
                " ORDER BY record_id"
            ),
            {"b": book_id, "iv": iv},
        ).fetchall()
    engine.dispose()
    if not rows:
        raise SystemExit(f"❌ {book_id} 的 active 索引无向量记录")
    dim = len(rows[0][2]) // 4
    return [(r[0], r[1], r[2]) for r in rows], iv, dim


def _unpack(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"{dim}f", blob))


def bench_bruteforce(
    engine, book_id: str, iv: str, dim: int, queries: list[bytes], top_k: int
) -> dict:
    store = BruteForceVectorStore(dimension=dim)
    # 预热（SQL 冷路径 + 首次解包）
    store.search(
        engine, query=_unpack(queries[0], dim), book_id=book_id, index_version_id=iv, top_k=top_k
    )
    times: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        store.search(
            engine, query=_unpack(q, dim), book_id=book_id, index_version_id=iv, top_k=top_k
        )
        times.append(time.perf_counter() - t0)
    return _summarize(times)


def bench_sqlitevec(
    records: list[tuple[int, str, bytes]], queries: list[bytes], dim: int, top_k: int
) -> dict:
    from novelcanon.storage.engine import create_db_engine

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = Path(tmp.name)
    engine = create_db_engine(db_path, enable_vec=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE embedding_records ("
                " record_id INTEGER PRIMARY KEY, raw_chunk_id TEXT,"
                " book_id TEXT, index_version_id TEXT, vector BLOB)"
            )
        )
    store = SqliteVecVectorStore(dimension=dim)

    # 重建：SQL 元数据 + vec0 虚表
    t0 = time.perf_counter()
    with engine.begin() as conn:
        for rid, chunk, blob in records:
            conn.execute(
                text(
                    "INSERT INTO embedding_records (record_id, raw_chunk_id, book_id,"
                    " index_version_id, vector) VALUES (:r, :c, 'b', 'iv', :v)"
                ),
                {"r": rid, "c": chunk, "v": blob},
            )
    rebuild_sql = time.perf_counter() - t0
    t0 = time.perf_counter()
    for rid, _chunk, blob in records:
        store.add(engine, record_id=rid, embedding=_unpack(blob, dim), book_id="b")
    rebuild_vec = time.perf_counter() - t0

    # 预热 + 查询
    store.search(
        engine, query=_unpack(queries[0], dim), book_id="b", index_version_id="iv", top_k=top_k
    )
    times: list[float] = []
    for q in queries:
        t0 = time.perf_counter()
        store.search(engine, query=_unpack(q, dim), book_id="b", index_version_id="iv", top_k=top_k)
        times.append(time.perf_counter() - t0)

    size_mb = db_path.stat().st_size / 1e6
    engine.dispose()
    db_path.unlink(missing_ok=True)
    return {
        **_summarize(times),
        "rebuild_sql_s": round(rebuild_sql, 3),
        "rebuild_vec_s": round(rebuild_vec, 3),
        "db_size_mb": round(size_mb, 2),
    }


def _summarize(times: list[float]) -> dict:
    times.sort()

    def pct(p: float) -> float:
        return times[min(len(times) - 1, int(len(times) * p))]

    return {
        "queries": len(times),
        "p50_ms": round(pct(0.50) * 1e3, 2),
        "p95_ms": round(pct(0.95) * 1e3, 2),
        "max_ms": round(times[-1] * 1e3, 2),
    }


def _env_info() -> str:
    """环境信息：sqlite-vec / SQLite / Python 版本（复审 P1：压测结果
    环境依赖——复测必须记录版本，否则数字不可比）。"""
    import platform
    import sqlite3

    import sqlite_vec

    return (
        f"python={platform.python_version()} sqlite={sqlite3.sqlite_version}"
        f" sqlite_vec={getattr(sqlite_vec, '__version__', 'unknown')}"
        f" platform={platform.platform()}"
    )


def main() -> None:
    from novelcanon.config.settings import AppSettings

    parser = argparse.ArgumentParser(description="向量检索压测")
    parser.add_argument("--book", default="book_cc")
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()

    print(f"环境：{_env_info()}")
    db_path = Path(AppSettings().db_path)
    records, iv, dim = load_records(db_path, args.book)
    print(
        f"数据：{args.book} {len(records)} chunks / {dim} 维 / top_k={args.top_k} / index={iv[:12]}"
    )

    rng = random.Random(42)
    queries = [records[rng.randrange(len(records))][2] for _ in range(args.queries)]

    real_engine = create_engine(f"sqlite:///{db_path}")
    print("\n[BruteForceVectorStore] 全扫描余弦（真实库）")
    bf = bench_bruteforce(real_engine, args.book, iv, dim, queries, args.top_k)
    print(
        f"  查询 {bf['queries']} 次 p50={bf['p50_ms']}ms p95={bf['p95_ms']}ms max={bf['max_ms']}ms"
    )

    print("\n[SqliteVecVectorStore] vec0 KNN（临时库重建）")
    sv = None
    try:
        sv = bench_sqlitevec(records, queries, dim, args.top_k)
        print(
            f"  重建: SQL {sv['rebuild_sql_s']}s + vec {sv['rebuild_vec_s']}s"
            f" | 查询 p50={sv['p50_ms']}ms p95={sv['p95_ms']}ms max={sv['max_ms']}ms"
            f" | 临时库体积 {sv['db_size_mb']}MB"
        )
    except Exception as exc:  # noqa: BLE001 —— 压测脚本报告扩展不可用
        print(f"  ❌ sqlite-vec 不可用：{exc}")

    print("\n[结论]")
    if sv is not None:
        ratio = bf["p95_ms"] / max(sv["p95_ms"], 0.001)
        # 三种情况动态生成（复审 P2）：ratio<1 代表 BruteForce 更快，
        # 不得仍输出「两者相当」；也不得无条件输出「无切换收益」——
        # 与 sqlite-vec 快 10× 的环境结果冲突。
        if ratio > 1.5:
            verdict = f"sqlite-vec 快 {ratio:.1f}×"
        elif ratio < 0.667:
            verdict = f"BruteForce 快 {1 / ratio:.1f}×（sqlite-vec 更慢）"
        else:
            verdict = f"两者相当（{ratio:.1f}×）"
        print(
            f"  短篇规模（{len(records)} chunks/{dim} 维）：BruteForce p95 {bf['p95_ms']}ms"
            f" vs sqlite-vec p95 {sv['p95_ms']}ms（{verdict}）"
        )
        print(
            "  ⚠️  sqlite-vec 性能环境依赖：不同 sqlite-vec 版本/存储介质差异可达 30×"
            "（复测环境 148ms vs 本机 4.6ms，重建 50.8s vs 0.86s）。"
            " 本结论仅在输出所示环境成立。"
        )
        print(
            "  决策：无论本次孰快，是否切换 sqlite-vec 都必须在**目标生产环境**"
            " 复测本脚本后再决定——切换前验证 profile/维数/metadata 过滤/重建/回滚。"
        )
    real_engine.dispose()


if __name__ == "__main__":
    main()
