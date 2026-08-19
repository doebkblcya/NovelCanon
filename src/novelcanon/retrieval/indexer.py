"""索引 service（阶段 03，§3.3/§7）。

build_index：全书 chunk 化 → raw_chunks/FTS/向量落库 → 原子激活新版本；
rebuild_chapter：单章内容修订时只重建该章（同一 active 版本内原子替换）；
配置（tokenizer/chunking）变化 → 新 chunking_version → 新版本全量重建。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, text

from novelcanon.retrieval.chunker import ChunkConfig, ChunkDraft, chunk_text, chunking_version_for
from novelcanon.retrieval.fts import insert_shadow_into, insert_trigram_into, remove_chunk_from
from novelcanon.retrieval.tokenizer import Tokenizer
from novelcanon.retrieval.vectorstore import Embedder, VectorStore
from novelcanon.schemas.ids import new_uuid_id
from novelcanon.storage.repository import Repository, now_iso


@dataclass(frozen=True)
class IndexResult:
    book_id: str
    index_version_id: str
    chunking_version: str
    chunk_count: int
    status: str


def get_active_index_version(engine: Engine, book_id: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM index_versions WHERE book_id = :b AND status = 'active'"),
                {"b": book_id},
            )
            .mappings()
            .fetchone()
        )
        return dict(row) if row else None


def build_index(
    engine: Engine,
    book_id: str,
    *,
    tokenizer: Tokenizer,
    embedder: Embedder,
    vector_store: VectorStore,
    config: ChunkConfig | None = None,
) -> IndexResult:
    """全书建索引并原子激活（旧 active → retired，新 → active）。"""
    if config is None:
        config = ChunkConfig()
    repo = Repository(engine)
    chapters = repo.list_chapters(book_id)
    full_text = repo.get_book_text(book_id)
    cver = chunking_version_for(tokenizer, config)
    index_version_id = new_uuid_id("idx")

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO index_versions (index_version_id, book_id, chunking_version,"
                " embedding_profile_id, status, created_at)"
                " VALUES (:iv, :b, :cver, :prof, 'building', :ts)"
            ),
            {
                "iv": index_version_id,
                "b": book_id,
                "cver": cver,
                "prof": vector_store.profile_id,
                "ts": now_iso(),
            },
        )

    chunk_count = 0
    for ch in chapters:
        ch_text = full_text[ch["char_start"] : ch["char_end"]]
        drafts = chunk_text(
            ch_text,
            source_chapter_id=ch["chapter_id"],
            observed_ordinal=ch["ordinal"],
            tokenizer=tokenizer,
            chunking_version=cver,
            config=config,
        )
        for draft in drafts:
            _write_chunk(engine, book_id, index_version_id, draft, embedder, vector_store)
            chunk_count += 1

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE index_versions SET status = 'retired'"
                " WHERE book_id = :b AND status = 'active'"
            ),
            {"b": book_id},
        )
        conn.execute(
            text("UPDATE index_versions SET status = 'active' WHERE index_version_id = :iv"),
            {"iv": index_version_id},
        )

    return IndexResult(
        book_id=book_id,
        index_version_id=index_version_id,
        chunking_version=cver,
        chunk_count=chunk_count,
        status="active",
    )


def rebuild_chapter(
    engine: Engine,
    book_id: str,
    chapter_id: str,
    *,
    tokenizer: Tokenizer,
    embedder: Embedder,
    vector_store: VectorStore,
    config: ChunkConfig | None = None,
) -> int:
    """单章重建：删除该章在 active 版本下的旧 chunk 后重切（§7：只重建该章）。"""
    if config is None:
        config = ChunkConfig()
    active = get_active_index_version(engine, book_id)
    if active is None:
        raise ValueError(f"book {book_id} 没有 active 索引，请先 build_index")

    repo = Repository(engine)
    full_text = repo.get_book_text(book_id)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT char_start, char_end FROM chapters WHERE chapter_id = :c"),
            {"c": chapter_id},
        ).fetchone()
    if row is None:
        raise ValueError(f"chapter {chapter_id} 不存在")
    ch_text = full_text[row[0] : row[1]]

    # 1. 删除旧产物（raw_chunks 级联删 embedding_records；FTS 手动删）
    with engine.begin() as conn:
        old_ids = (
            conn.execute(
                text(
                    "SELECT raw_chunk_id FROM raw_chunks WHERE source_chapter_id = :c"
                    " AND index_version_id = :iv"
                ),
                {"c": chapter_id, "iv": active["index_version_id"]},
            )
            .scalars()
            .all()
        )
        for cid in old_ids:
            remove_chunk_from(conn, cid)
        conn.execute(
            text("DELETE FROM raw_chunks WHERE source_chapter_id = :c AND index_version_id = :iv"),
            {"c": chapter_id, "iv": active["index_version_id"]},
        )

    # 2. 重切写入
    drafts = chunk_text(
        ch_text,
        source_chapter_id=chapter_id,
        observed_ordinal=_ordinal_of(engine, chapter_id),
        tokenizer=tokenizer,
        chunking_version=active["chunking_version"],
        config=config,
    )
    for draft in drafts:
        _write_chunk(engine, book_id, active["index_version_id"], draft, embedder, vector_store)
    return len(drafts)


def _ordinal_of(engine: Engine, chapter_id: str) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT ordinal FROM chapters WHERE chapter_id = :c"), {"c": chapter_id}
        ).fetchone()
    if row is None:
        raise ValueError(f"chapter {chapter_id} 不存在")
    return row[0]


def _write_chunk(
    engine: Engine,
    book_id: str,
    index_version_id: str,
    draft: ChunkDraft,
    embedder: Embedder,
    vector_store: VectorStore,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR REPLACE INTO raw_chunks (raw_chunk_id, source_chapter_id,"
                " chunking_version, index_version_id, token_start, token_end, char_start,"
                " char_end, token_count, content, content_hash, embedding_profile_id,"
                " observed_ordinal)"
                " VALUES (:id, :ch, :cver, :iv, :ts, :te, :cs, :ce, :tc, :content, :hash,"
                " :prof, :ord)"
            ),
            {
                "id": draft.raw_chunk_id,
                "ch": draft.source_chapter_id,
                "cver": draft.chunking_version,
                "iv": index_version_id,
                "ts": draft.token_start,
                "te": draft.token_end,
                "cs": draft.char_start,
                "ce": draft.char_end,
                "tc": draft.token_count,
                "content": draft.content,
                "hash": draft.content_hash,
                "prof": vector_store.profile_id,
                "ord": draft.observed_ordinal,
            },
        )
        rec = conn.execute(
            text(
                "INSERT INTO embedding_records (raw_chunk_id, book_id, profile_id,"
                " index_version_id, created_at) VALUES (:chunk, :book, :prof, :iv, :ts)"
            ),
            {
                "chunk": draft.raw_chunk_id,
                "book": book_id,
                "prof": vector_store.profile_id,
                "iv": index_version_id,
                "ts": now_iso(),
            },
        )
        record_id = rec.lastrowid
        insert_shadow_into(
            conn,
            raw_chunk_id=draft.raw_chunk_id,
            book_id=book_id,
            ordinal=draft.observed_ordinal,
            content=draft.content,
        )
        insert_trigram_into(
            conn,
            raw_chunk_id=draft.raw_chunk_id,
            book_id=book_id,
            ordinal=draft.observed_ordinal,
            content=draft.content,
        )
    if record_id is not None:
        vector_store.add(
            engine, record_id=record_id, embedding=embedder.embed(draft.content), book_id=book_id
        )
