"""FTS 检索（阶段 03）。

- 预分词影子列（jieba 空格拼接，ADR 决策）为默认方法；
- trigram 作为对照/补充（子串召回，二字人名之外）；
- 所有查询必须先限定 book_id（§3.3/§11）。
"""

from __future__ import annotations

import re

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

FTS_TOKENIZER_VERSION = "jieba-v1"

# FTS5 MATCH 会把 `?` `*` `"` `()` `:` 等当语法符号——查询词只允许
# 中英文/数字，纯标点 token 必须丢弃（用户问题天然带标点）。
_FTS_QUERY_TOKEN = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


def segment_ws(text: str) -> str:
    """jieba 分词后空格拼接（影子列内容与查询词用同版本分词器）。"""
    import jieba  # type: ignore[import-untyped]

    return " ".join(jieba.cut(text))


def _fts_query_terms(query: str) -> str:
    """jieba 分词 → 丢弃纯标点 token → 每个词双引号包裹（FTS5 字面量）。

    冒烟实测：用户问题「…是什么人物？」经 jieba 切出 `？` 独立 token，
    直接拼进 MATCH 触发 fts5: syntax error near "?" → 500。索引侧影子列
    内容含标点无害（unicode61 索引时忽略），只清洗查询侧。
    双引号包裹使词按字面匹配（隐含 AND，与原实现语义一致），并防御
    token 内含 AND/OR/NOT 等 FTS 关键字被误解析。
    """
    terms: list[str] = []
    for tok in re.split(r"\s+", segment_ws(query)):
        tok = tok.strip()
        if not tok or not _FTS_QUERY_TOKEN.search(tok):
            continue  # 纯标点 token 丢弃（FTS5 语法字符）
        terms.append(f'"{tok.replace(chr(34), "")}"')
    return " ".join(terms)


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


def search_shadow(
    engine: Engine,
    *,
    query: str,
    book_id: str,
    limit: int = 20,
    cutoff: int | None = None,
) -> list[dict]:
    """jieba 影子列检索：查询词同版本分词，FTS5 MATCH + book 限定。

    cutoff（P1）：observed_ordinal 在 SQL LIMIT 前过滤——后期高排名候选
    不会挤占截止点前的相关结果（避免候选窗口截断后假性无结果）。
    """
    query_ws = _fts_query_terms(query)
    if not query_ws:
        return []  # 全标点/空查询：无词可查，返回空（不触发 MATCH 语法错误）
    params: dict[str, object] = {"q": query_ws, "b": book_id, "lim": limit}
    cutoff_sql = ""
    if cutoff is not None:
        cutoff_sql = " AND observed_ordinal <= :cutoff"
        params["cutoff"] = cutoff
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT raw_chunk_id, observed_ordinal, bm25(fts_chunks) AS score"
                    " FROM fts_chunks WHERE fts_chunks MATCH :q AND book_id = :b"
                    f"{cutoff_sql} ORDER BY score LIMIT :lim"
                ),
                params,
            )
            .mappings()
            .fetchall()
        )
    return [dict(r) for r in rows]


def search_trigram(
    engine: Engine,
    *,
    query: str,
    book_id: str,
    limit: int = 20,
    cutoff: int | None = None,
) -> list[dict]:
    """trigram 检索：原文子串匹配（如专名/短查询）。

    cutoff（P1）：同 search_shadow，SQL LIMIT 前过滤 ordinal。
    """
    query_clean = query.replace(chr(34), "").strip()
    if not query_clean:
        return []  # 空/纯引号查询：无内容可查
    params: dict[str, object] = {"q": f'"{query_clean}"', "b": book_id, "lim": limit}
    cutoff_sql = ""
    if cutoff is not None:
        cutoff_sql = " AND observed_ordinal <= :cutoff"
        params["cutoff"] = cutoff
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT raw_chunk_id, observed_ordinal, bm25(fts_chunks_trigram) AS score"
                    " FROM fts_chunks_trigram WHERE fts_chunks_trigram MATCH :q AND book_id = :b"
                    f"{cutoff_sql} ORDER BY score LIMIT :lim"
                ),
                params,
            )
            .mappings()
            .fetchall()
        )
    return [dict(r) for r in rows]
