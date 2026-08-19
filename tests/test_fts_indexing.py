"""阶段 03：FTS / 向量 / 索引版本 / 单章重建黄金测试。"""

from sqlalchemy import Engine, text

from novelcanon.retrieval.chunker import ChunkConfig
from novelcanon.retrieval.fts import search_shadow, search_trigram
from novelcanon.retrieval.indexer import build_index, get_active_index_version, rebuild_chapter
from novelcanon.retrieval.tokenizer import FakeTokenizer
from novelcanon.retrieval.vectorstore import (
    BruteForceVectorStore,
    FakeEmbedder,
    SqliteVecVectorStore,
)
from novelcanon.storage.repository import Repository

TOKENIZER = FakeTokenizer()


def _stack(engine: Engine, book_id: str, *, dimension: int = 8) -> dict:
    del engine, book_id
    return {
        "tokenizer": TOKENIZER,
        "embedder": FakeEmbedder(dimension),
        "vector_store": BruteForceVectorStore(dimension),
    }


def test_build_index_and_fts_recall(imported_book) -> None:
    """FTS 影子列可召回黄金专名/原句（03 验证项）。"""
    engine, book_id = imported_book
    result = build_index(engine, book_id, **_stack(engine, book_id))
    assert result.status == "active"
    assert result.chunk_count >= 3

    # 专名召回（jieba 影子列）
    hits = search_shadow(engine, query="青云宗", book_id=book_id)
    assert hits, "专名「青云宗」应被召回"
    # 原句召回
    hits2 = search_shadow(engine, query="三年之约", book_id=book_id)
    assert hits2, "「三年之约」应被召回"


def test_fts_trigram_recall(imported_book) -> None:
    engine, book_id = imported_book
    build_index(engine, book_id, **_stack(engine, book_id))
    hits = search_trigram(engine, query="青莲地心火", book_id=book_id)
    assert hits, "trigram 应召回子串「青莲地心火」"


def test_multi_book_fts_isolation(imported_book, epub_file) -> None:
    """多书查询不返回其他 book_id（03 验证项）。"""
    from novelcanon.ingestion.service import import_book

    engine, book_a = imported_book
    build_index(engine, book_a, **_stack(engine, book_a))

    book_b = import_book(engine, epub_file).book_id  # 重新导入同文件 → 幂等新 book
    build_index(engine, book_b, **_stack(engine, book_b))

    hits_a = search_shadow(engine, query="青云宗", book_id=book_a)
    hits_b = search_shadow(engine, query="青云宗", book_id=book_b)
    assert hits_a and hits_b
    ids_a = {h["raw_chunk_id"] for h in hits_a}
    ids_b = {h["raw_chunk_id"] for h in hits_b}
    assert ids_a.isdisjoint(ids_b), "不同书的 chunk 不得互相返回"


def test_vector_search_bruteforce(imported_book) -> None:
    engine, book_id = imported_book
    build_index(engine, book_id, **_stack(engine, book_id))
    active = get_active_index_version(engine, book_id)
    assert active is not None

    store = BruteForceVectorStore(8)
    embedder = FakeEmbedder(8)
    hits = store.search(
        engine,
        query=embedder.embed("青云宗 石碑"),
        book_id=book_id,
        index_version_id=active["index_version_id"],
        top_k=5,
    )
    assert hits, "BruteForce 向量检索应返回结果"
    assert all(h.raw_chunk_id.startswith("chunk_") for h in hits)


def test_sqlite_vec_store(imported_book) -> None:
    """sqlite-vec 后端（vec0 虚表）可用；未装 vec extra 时跳过。"""
    from pathlib import Path

    import pytest

    pytest.importorskip("sqlite_vec")
    from novelcanon.storage.engine import create_db_engine

    engine, book_id = imported_book
    # vec0 需要加载扩展的连接（ADR-0006）
    vec_engine = create_db_engine(Path(engine.url.database), enable_vec=True)
    try:
        build_index(
            vec_engine,
            book_id,
            tokenizer=TOKENIZER,
            embedder=FakeEmbedder(4),
            vector_store=SqliteVecVectorStore(4),
        )
        active = get_active_index_version(vec_engine, book_id)
        hits = SqliteVecVectorStore(4).search(
            vec_engine,
            query=FakeEmbedder(4).embed("青莲地心火"),
            book_id=book_id,
            index_version_id=active["index_version_id"],
            top_k=3,
        )
        assert hits
    finally:
        vec_engine.dispose()


def test_index_version_switch(imported_book) -> None:
    """chunk 配置变化 → 新版本全量重建，旧版本 retired（§3.3 原子切换）。"""
    engine, book_id = imported_book
    v1 = build_index(engine, book_id, **_stack(engine, book_id))
    v2 = build_index(
        engine,
        book_id,
        tokenizer=TOKENIZER,
        embedder=FakeEmbedder(8),
        vector_store=BruteForceVectorStore(8),
        config=ChunkConfig(target_tokens=100),
    )
    assert v1.index_version_id != v2.index_version_id
    with engine.connect() as conn:
        statuses = dict(
            conn.execute(
                text("SELECT index_version_id, status FROM index_versions WHERE book_id = :b"),
                {"b": book_id},
            ).fetchall()
        )
    assert statuses[v1.index_version_id] == "retired"
    assert statuses[v2.index_version_id] == "active"


def test_rebuild_chapter_only(imported_book) -> None:
    """修改一章只重建该章（03 验证项），其他章 chunk 不变。"""
    engine, book_id = imported_book
    repo = Repository(engine)
    chapters = repo.list_chapters(book_id)
    target = chapters[1]
    other = chapters[0]

    build_index(engine, book_id, **_stack(engine, book_id))

    def chunk_hashes(chapter_id: str) -> set[tuple[str, str]]:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT raw_chunk_id, content_hash FROM raw_chunks WHERE source_chapter_id = :c"
                ),
                {"c": chapter_id},
            ).fetchall()
            return {(r[0], r[1]) for r in rows}

    before_other = chunk_hashes(other["chapter_id"])
    before_target = chunk_hashes(target["chapter_id"])

    # 同长度替换一章内容（修订：hash 变化但 offset 不变）
    full = repo.get_book_text(book_id)
    span_len = target["char_end"] - target["char_start"]
    new_body = "第一章 雨夜惊变\n修订后的正文内容。修订后的正文内容。"[:span_len].ljust(span_len)
    new_text = full[: target["char_start"]] + new_body + full[target["char_end"] :]
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE books SET normalized_text = :t WHERE book_id = :b"),
            {"t": new_text, "b": book_id},
        )
        conn.execute(
            text("UPDATE chapters SET content_hash = :h WHERE chapter_id = :c"),
            {"h": "changed", "c": target["chapter_id"]},
        )

    count = rebuild_chapter(engine, book_id, target["chapter_id"], **_stack(engine, book_id))
    assert count >= 1

    assert chunk_hashes(other["chapter_id"]) == before_other, "其他章 chunk 不得变化"
    assert chunk_hashes(target["chapter_id"]) != before_target, "目标章 chunk 已重建"
