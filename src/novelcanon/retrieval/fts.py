"""FTS 检索（阶段 03）。

- 预分词影子列（jieba 空格拼接，ADR 决策）为默认方法；
- trigram 作为对照/补充（子串召回，二字人名之外）；
- 所有查询必须先限定 book_id（§3.3/§11）。
"""

from __future__ import annotations

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

FTS_TOKENIZER_VERSION = "jieba-v1"


def segment_ws(text: str) -> str:
    """jieba 分词后空格拼接（影子列内容与查询词用同版本分词器）。"""
    import jieba  # type: ignore[import-untyped]

    return " ".join(jieba.cut(text))


def insert_shadow(
    engine: Engine, *, raw_chunk_id: str, book_id: str, ordinal: int, content: str
) -> None:
    with engine.begin() as conn:
        insert_shadow_into(
            conn, raw_chunk_id=raw_chunk_id, book_id=book_id, ordinal=ordinal, content=content
        )


def insert_shadow_into(
    conn: Connection, *, raw_chunk_id: str, book_id: str, ordinal: int, content: str
) -> None:
    """影子列写入（复用调用方事务连接，避免嵌套 begin 锁冲突）。"""
    conn.execute(
        text(
            "INSERT OR REPLACE INTO fts_chunks"
            " (raw_chunk_id, book_id, observed_ordinal, content_ws)"
            " VALUES (:id, :book, :ord, :ws)"
        ),
        {"id": raw_chunk_id, "book": book_id, "ord": ordinal, "ws": segment_ws(content)},
    )


def insert_trigram(
    engine: Engine, *, raw_chunk_id: str, book_id: str, ordinal: int, content: str
) -> None:
    with engine.begin() as conn:
        insert_trigram_into(
            conn, raw_chunk_id=raw_chunk_id, book_id=book_id, ordinal=ordinal, content=content
        )


def insert_trigram_into(
    conn: Connection, *, raw_chunk_id: str, book_id: str, ordinal: int, content: str
) -> None:
    conn.execute(
        text(
            "INSERT OR REPLACE INTO fts_chunks_trigram"
            " (raw_chunk_id, book_id, observed_ordinal, content)"
            " VALUES (:id, :book, :ord, :content)"
        ),
        {"id": raw_chunk_id, "book": book_id, "ord": ordinal, "content": content},
    )


def remove_chunk(engine: Engine, raw_chunk_id: str) -> None:
    """删除某 chunk 的 FTS 行（单章重建用）。"""
    with engine.begin() as conn:
        remove_chunk_from(conn, raw_chunk_id)


def remove_chunk_from(conn: Connection, raw_chunk_id: str) -> None:
    conn.execute(text("DELETE FROM fts_chunks WHERE raw_chunk_id = :id"), {"id": raw_chunk_id})
    conn.execute(
        text("DELETE FROM fts_chunks_trigram WHERE raw_chunk_id = :id"),
        {"id": raw_chunk_id},
    )


def search_shadow(engine: Engine, *, query: str, book_id: str, limit: int = 20) -> list[dict]:
    """jieba 影子列检索：查询词同版本分词，FTS5 MATCH + book 限定。"""
    query_ws = segment_ws(query)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT raw_chunk_id, observed_ordinal, bm25(fts_chunks) AS score"
                    " FROM fts_chunks WHERE fts_chunks MATCH :q AND book_id = :b"
                    " ORDER BY score LIMIT :lim"
                ),
                {"q": query_ws, "b": book_id, "lim": limit},
            )
            .mappings()
            .fetchall()
        )
    return [dict(r) for r in rows]


def search_trigram(engine: Engine, *, query: str, book_id: str, limit: int = 20) -> list[dict]:
    """trigram 检索：原文子串匹配（如专名/短查询）。"""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT raw_chunk_id, observed_ordinal, bm25(fts_chunks_trigram) AS score"
                    " FROM fts_chunks_trigram WHERE fts_chunks_trigram MATCH :q AND book_id = :b"
                    " ORDER BY score LIMIT :lim"
                ),
                # trigram 查询用引号包裹做短语/子串匹配
                {"q": f'"{query}"', "b": book_id, "lim": limit},
            )
            .mappings()
            .fetchall()
        )
    return [dict(r) for r in rows]
