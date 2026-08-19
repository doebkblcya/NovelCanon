"""检索（阶段 03）：raw chunk、FTS（影子列/trigram）、向量抽象。"""

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
    "Embedder",
    "FTS_TOKENIZER_VERSION",
    "FakeEmbedder",
    "FakeTokenizer",
    "IndexResult",
    "SearchHit",
    "SqliteVecVectorStore",
    "TiktokenAdapter",
    "Tokenizer",
    "VectorStore",
    "build_index",
    "chunk_text",
    "chunking_version_for",
    "get_active_index_version",
    "insert_shadow",
    "insert_trigram",
    "rebuild_chapter",
    "remove_chunk",
    "search_shadow",
    "search_trigram",
    "segment_ws",
]
