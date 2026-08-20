"""检索 service（阶段 10 §2–§3，docs/implementation/10）。

- 向量检索：运行时验证 profile 与维数；Top-K 前完成 book/profile 隔离
  （向量后端按 book 过滤，普通表回读仍限定 book）；记录 record ID /
  raw_chunk_id 后从普通表读取元数据（章节、ordinal、原文 span）；
  索引版本显式指定或取 active，版本切换与 raw chunk 一致。
- 混合检索：FTS（影子列 + trigram）与向量各自产生候选，融合前应用
  book/cutoff 过滤，RRF 按稳定 raw_chunk_id 去重融合，保存各路线贡献。

说明：RRF 只用排名不看分数，规避了向量后端的相似度/距离尺度差异
（BruteForce 余弦相似度 vs sqlite-vec distance）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from novelcanon.retrieval.fts import search_shadow, search_trigram
from novelcanon.retrieval.indexer import get_active_index_version
from novelcanon.retrieval.rrf import RRF_PARAMS_VERSION, fts_query_candidates, rrf_fuse
from novelcanon.retrieval.vectorstore import Embedder, VectorStore

_TOP_K_MULTIPLIER = 4  # 候选放宽系数：融合前各路线取 top_k×N 再 RRF


@dataclass(frozen=True)
class RetrievalHit:
    """一条检索命中：raw chunk + 元数据（原文 span 与章节定位）。"""

    raw_chunk_id: str
    score: float
    source_chapter_id: str
    observed_ordinal: int
    char_start: int
    char_end: int
    content: str
    content_hash: str
    routes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HybridResult:
    """混合检索结果：融合命中 + 版本与路线贡献诊断（10 §3）。"""

    hits: list[RetrievalHit]
    index_version_id: str
    rrf_params_version: str = RRF_PARAMS_VERSION
    contributions: dict[str, int] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


class RetrievalService:
    """book_id 绑定的检索服务（构造时绑定，所有查询强制限定本书）。"""

    def __init__(
        self,
        engine: Engine,
        book_id: str,
        *,
        embedder: Embedder,
        vector_store: VectorStore,
        index_version_id: str | None = None,
    ) -> None:
        self._engine = engine
        self._book_id = book_id
        self._embedder = embedder
        self._vector_store = vector_store
        self._index_version_id = index_version_id

    # ── 索引版本与 profile 验证 ─────────────────────────────────

    def active_index(self) -> dict:
        """当前生效的索引版本（显式指定或取 active；验证归属本书）。"""
        if self._index_version_id is not None:
            with self._engine.connect() as conn:
                row = (
                    conn.execute(
                        text(
                            "SELECT * FROM index_versions"
                            " WHERE index_version_id = :iv AND book_id = :b"
                        ),
                        {"iv": self._index_version_id, "b": self._book_id},
                    )
                    .mappings()
                    .fetchone()
                )
            if row is None:
                raise ValueError(
                    f"index_version {self._index_version_id} 不存在或不属于"
                    f" book={self._book_id}"
                )
            return dict(row)
        index = get_active_index_version(self._engine, self._book_id)
        if index is None:
            raise ValueError(f"book={self._book_id} 没有 active 索引，请先 build_index")
        return index

    def _verify_profile(self, index: dict) -> None:
        """运行时验证（10 §2）：embedding profile 与维数一致。

        - embedder.profile_id 必须等于索引声明的 embedding_profile_id；
        - vector_store 维数必须等于 embedder 维数；
        - 索引下必须有该 profile 的 embedding 记录（无向量则不可检索）。
        """
        index_profile = index.get("embedding_profile_id")
        if index_profile != self._embedder.profile_id:
            raise ValueError(
                f"embedding profile 不匹配：索引={index_profile!r}"
                f" embedder={self._embedder.profile_id!r}（索引版本切换后需重建索引）"
            )
        if self._vector_store.dimension != self._embedder.dimension:
            raise ValueError(
                f"向量维数不匹配：embedder={self._embedder.dimension}"
                f" store={self._vector_store.dimension}"
            )
        with self._engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM embedding_records"
                    " WHERE index_version_id = :iv AND profile_id = :p AND book_id = :b"
                ),
                {
                    "iv": index["index_version_id"],
                    "p": self._embedder.profile_id,
                    "b": self._book_id,
                },
            ).scalar()
        if not count:
            raise ValueError(
                f"index={index['index_version_id']} 没有 profile="
                f"{self._embedder.profile_id} 的 embedding 记录"
            )

    # ── 向量检索 ───────────────────────────────────────────────

    def search_vectors(self, query: str, *, top_k: int = 10) -> list[RetrievalHit]:
        """向量检索：profile 验证 → book 隔离 Top-K → 元数据回读。"""
        index = self.active_index()
        self._verify_profile(index)
        query_vec = self._embedder.embed(query)
        hits = self._vector_store.search(
            self._engine,
            query=query_vec,
            book_id=self._book_id,
            index_version_id=index["index_version_id"],
            top_k=top_k,
        )
        return self._hydrate(
            [h.raw_chunk_id for h in hits],
            scores={h.raw_chunk_id: h.score for h in hits},
            routes=["vector"],
        )

    # ── FTS 检索 ───────────────────────────────────────────────

    def search_fts(
        self, query: str, *, limit: int = 20, cutoff: int | None = None
    ) -> list[RetrievalHit]:
        """FTS 检索：影子列 + trigram 合并（cutoff 在 SQL 内过滤）。"""
        shadow = [
            h
            for h in search_shadow(
                self._engine, query=query, book_id=self._book_id, limit=limit
            )
            if cutoff is None or h["observed_ordinal"] <= cutoff
        ]
        trigram = [
            h
            for h in search_trigram(
                self._engine, query=query, book_id=self._book_id, limit=limit
            )
            if cutoff is None or h["observed_ordinal"] <= cutoff
        ]
        ordered = fts_query_candidates(shadow, trigram)
        scores: dict[str, float] = {}
        for h in shadow:
            scores.setdefault(h["raw_chunk_id"], float(h["score"]))
        return self._hydrate(ordered, scores=scores, routes=["fts"])

    # ── 混合检索（RRF）─────────────────────────────────────────

    def hybrid_search(
        self,
        query: str,
        *,
        top_k: int = 10,
        cutoff: int | None = None,
    ) -> HybridResult:
        """FTS + 向量 RRF 融合（融合前应用 book/cutoff 过滤，10 §3）。

        - 向量候选在融合前按元数据 ordinal 过滤 cutoff（vec0 无法在
          SQL 内过滤 ordinal）；
        - 各路线候选放宽到 top_k×4，RRF 融合后取 top_k；
        - 返回各路线贡献（命中数）与诊断（候选规模）。
        """
        index = self.active_index()
        fts_hits = self.search_fts(query, limit=top_k * _TOP_K_MULTIPLIER, cutoff=cutoff)
        vec_hits = self.search_vectors(query, top_k=top_k * _TOP_K_MULTIPLIER)
        if cutoff is not None:
            vec_hits = [h for h in vec_hits if h.observed_ordinal <= cutoff]

        fused = rrf_fuse(
            {
                "fts": [h.raw_chunk_id for h in fts_hits],
                "vector": [h.raw_chunk_id for h in vec_hits],
            }
        )
        top = fused[:top_k]
        scores = {f.raw_chunk_id: f.score for f in top}
        routes = {f.raw_chunk_id: f.routes for f in top}
        hits = self._hydrate([f.raw_chunk_id for f in top], scores=scores, routes_map=routes)
        contributions = {"fts": len({h.raw_chunk_id for h in fts_hits})}
        contributions["vector"] = len({h.raw_chunk_id for h in vec_hits})
        return HybridResult(
            hits=hits,
            index_version_id=index["index_version_id"],
            contributions=contributions,
            diagnostics={
                "fts_candidates": len({h.raw_chunk_id for h in fts_hits}),
                "vector_candidates": len({h.raw_chunk_id for h in vec_hits}),
                "fused_candidates": len(fused),
                "k": 60,
            },
        )

    # ── 元数据回读（普通表，稳定 raw chunk）────────────────────

    def _hydrate(
        self,
        raw_chunk_ids: list[str],
        *,
        scores: dict[str, float] | None = None,
        routes: list[str] | None = None,
        routes_map: dict[str, list[str]] | None = None,
    ) -> list[RetrievalHit]:
        if not raw_chunk_ids:
            return []
        placeholders = ", ".join(f":c{n}" for n in range(len(raw_chunk_ids)))
        params: dict[str, object] = {f"c{n}": c for n, c in enumerate(raw_chunk_ids)}
        params["book"] = self._book_id
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT raw_chunk_id, source_chapter_id, observed_ordinal,"
                        " char_start, char_end, content, content_hash FROM raw_chunks"
                        f" WHERE raw_chunk_id IN ({placeholders})"
                        " AND EXISTS (SELECT 1 FROM index_versions iv"
                        "   WHERE iv.index_version_id = raw_chunks.index_version_id"
                        "   AND iv.book_id = :book)"
                    ),
                    params,
                )
                .mappings()
                .fetchall()
            )
        by_id = {r["raw_chunk_id"]: dict(r) for r in rows}
        out: list[RetrievalHit] = []
        for cid in raw_chunk_ids:
            meta = by_id.get(cid)
            if meta is None:
                continue  # 防御：索引版本切换后孤儿 chunk 直接跳过
            out.append(
                RetrievalHit(
                    raw_chunk_id=cid,
                    score=scores.get(cid, 0.0) if scores else 0.0,
                    source_chapter_id=meta["source_chapter_id"],
                    observed_ordinal=meta["observed_ordinal"],
                    char_start=meta["char_start"],
                    char_end=meta["char_end"],
                    content=meta["content"],
                    content_hash=meta["content_hash"],
                    routes=routes_map.get(cid, routes or []) if routes_map else (routes or []),
                )
            )
        return out
