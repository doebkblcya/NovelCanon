"""向量存储（ADR-0006）。

sqlite-vec 的 vec0 以 JSON 数组字符串承载向量参数。

VectorStore Protocol：
- BruteForceVectorStore：embedding_records.vector BLOB 全扫描余弦（测试/Pilot 基线）；
- SqliteVecVectorStore：vec0 虚表（生产候选），按维数独立建表、book_id 进 metadata。

确定性 FakeEmbedder 用于无真实 embedding 时验证版本切换与过滤。
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Engine, text


@dataclass(frozen=True)
class SearchHit:
    raw_chunk_id: str
    score: float


class Embedder(Protocol):
    profile_id: str
    dimension: int

    def embed(self, text: str) -> list[float]: ...


class FakeEmbedder:
    """确定性伪 embedding：文本 hash 播种 → 归一化向量。"""

    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension
        self.profile_id = f"fake-embed-v{dimension}"

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [
            ((digest[i % len(digest)] + i * 7) % 251 - 125) / 125.0 for i in range(self.dimension)
        ]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


class VectorStore(Protocol):
    """向量后端：add 只管向量本体；embedding_records 元数据行由 indexer 统一插入。"""

    profile_id: str
    dimension: int

    def add(
        self,
        engine: Engine,
        *,
        record_id: int,
        embedding: list[float],
        book_id: str,
    ) -> None: ...

    def search(
        self,
        engine: Engine,
        *,
        query: list[float],
        book_id: str,
        index_version_id: str,
        top_k: int,
    ) -> list[SearchHit]: ...


class BruteForceVectorStore:
    """余弦全扫描（测试/Pilot 基线）：向量存 embedding_records.vector。"""

    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension
        self.profile_id = f"bruteforce-{dimension}"

    def add(
        self,
        engine: Engine,
        *,
        record_id: int,
        embedding: list[float],
        book_id: str,
    ) -> None:
        del book_id
        if len(embedding) != self.dimension:
            raise ValueError(f"向量维数 {len(embedding)} != {self.dimension}")
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE embedding_records SET vector = :vec WHERE record_id = :rid"),
                {"vec": _pack(embedding), "rid": record_id},
            )

    def search(
        self,
        engine: Engine,
        *,
        query: list[float],
        book_id: str,
        index_version_id: str,
        top_k: int,
    ) -> list[SearchHit]:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT raw_chunk_id, vector FROM embedding_records"
                    " WHERE book_id = :b AND index_version_id = :iv AND vector IS NOT NULL"
                ),
                {"b": book_id, "iv": index_version_id},
            ).fetchall()
        scored = [SearchHit(raw_chunk_id=r[0], score=_cosine(query, _unpack(r[1]))) for r in rows]
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]


class SqliteVecVectorStore:
    """sqlite-vec vec0 虚表（生产候选，锁 0.1.9）。

    - 每种向量维数使用独立 vec0 表（vec_chunks_{dim}）；
    - book_id 放入 metadata 列（vec0 支持 KNN 前过滤）；
    - 连接必须经 create_db_engine(enable_vec=True)（扩展加载，ADR-0006）。
    """

    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension
        self.profile_id = f"sqlite-vec-{dimension}"
        self._table = f"vec_chunks_{dimension}"

    def _ensure_table(self, engine: Engine) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._table} USING vec0("
                    f" embedding FLOAT[{self.dimension}], book_id INTEGER)"
                )
            )

    def add(
        self,
        engine: Engine,
        *,
        record_id: int,
        embedding: list[float],
        book_id: str,
    ) -> None:
        if len(embedding) != self.dimension:
            raise ValueError(f"向量维数 {len(embedding)} != {self.dimension}")
        self._ensure_table(engine)
        # book_id 在 vec0 中以整数 partition 传入；元数据落普通表（indexer 已插入）
        bucket = int(hashlib.sha256(book_id.encode()).hexdigest()[:8], 16)
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {self._table} (rowid, book_id, embedding)"
                    " VALUES (:rid, :bucket, :vec)"
                ),
                # vec0 以 JSON 数组字符串承载向量
                {"rid": record_id, "bucket": bucket, "vec": json.dumps(embedding)},
            )

    def search(
        self,
        engine: Engine,
        *,
        query: list[float],
        book_id: str,
        index_version_id: str,
        top_k: int,
    ) -> list[SearchHit]:
        self._ensure_table(engine)
        bucket = int(hashlib.sha256(book_id.encode()).hexdigest()[:8], 16)
        # vec0 的 MATCH / k 不支持绑定参数（需字面量）；
        # query 为 JSON 数字数组、bucket/k 为整数，无注入风险。
        q = json.dumps(query)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT rowid, distance FROM {self._table}"
                    f" WHERE embedding MATCH '{q}' AND book_id = {bucket} AND k = {top_k}"
                )
            ).fetchall()
            if not rows:
                return []
            ids = [r[0] for r in rows]
            dist = {r[0]: r[1] for r in rows}
            placeholders = ",".join(f":i{n}" for n in range(len(ids)))
            params: dict[str, object] = {f"i{n}": v for n, v in enumerate(ids)}
            params["iv"] = index_version_id
            meta = conn.execute(
                text(
                    "SELECT record_id, raw_chunk_id FROM embedding_records"
                    f" WHERE record_id IN ({placeholders}) AND index_version_id = :iv"
                ),
                params,
            ).fetchall()
        return [
            SearchHit(raw_chunk_id=rid, score=float(dist[record_id]))
            for record_id, rid in meta
            if record_id in dist
        ]
