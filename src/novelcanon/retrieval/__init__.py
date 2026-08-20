"""检索（阶段 03/10）：raw chunk、FTS、向量、RRF 混合检索。"""

from novelcanon.retrieval.chunker import ChunkConfig, ChunkDraft, chunk_text, chunking_version_for
from novelcanon.retrieval.fts import (
    FTS_TOKENIZER_VERSION,
    insert_shadow,
    insert_trigram,
    remove_chunk,
    search_shadow,
    search_trigram,
    segment_ws,
)
from novelcanon.retrieval.indexer import (
    IndexResult,
    build_index,
    get_active_index_version,
    rebuild_chapter,
)
from novelcanon.retrieval.rrf import (
    DEFAULT_RRF_K,
    RRF_PARAMS_VERSION,
    FusedRank,
    fts_query_candidates,
    rrf_fuse,
)
from novelcanon.retrieval.service import HybridResult, RetrievalHit, RetrievalService
from novelcanon.retrieval.tokenizer import FakeTokenizer, TiktokenAdapter, Tokenizer
from novelcanon.retrieval.vectorstore import (
    BruteForceVectorStore,
    Embedder,
    FakeEmbedder,
    SearchHit,
    SqliteVecVectorStore,
    VectorStore,
)

__all__ = [
    "BruteForceVectorStore",
    "ChunkConfig",
    "ChunkDraft",
    "DEFAULT_RRF_K",
    "Embedder",
    "FTS_TOKENIZER_VERSION",
    "FakeEmbedder",
    "FakeTokenizer",
    "FusedRank",
    "HybridResult",
    "IndexResult",
    "RRF_PARAMS_VERSION",
    "RetrievalHit",
    "RetrievalService",
    "SearchHit",
    "SqliteVecVectorStore",
    "TiktokenAdapter",
    "Tokenizer",
    "VectorStore",
    "build_index",
    "chunk_text",
    "chunking_version_for",
    "fts_query_candidates",
    "get_active_index_version",
    "insert_shadow",
    "insert_trigram",
    "rebuild_chapter",
    "remove_chunk",
    "rrf_fuse",
    "search_shadow",
    "search_trigram",
    "segment_ws",
]
