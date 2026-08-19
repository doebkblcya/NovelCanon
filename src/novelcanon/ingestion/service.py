"""导入 service：格式适配器 → ParsedBook → 落库（阶段 03）。

EPUB 优先（目录/卷结构），TXT 为兜底；两者输出同一 ParsedBook 结构，
导入后统一走规范化文本与章节契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from novelcanon.ingestion.epub import ParsedBook, parse_epub
from novelcanon.ingestion.txt import parse_txt
from novelcanon.schemas.ids import new_uuid_id
from novelcanon.storage.repository import Repository

_SUPPORTED = {".epub": parse_epub, ".txt": parse_txt, ".md": parse_txt}


@dataclass(frozen=True)
class ImportResult:
    book_id: str
    title: str
    source_format: str
    chapter_count: int
    volume_count: int


def import_book(engine: Engine, path: Path, *, book_id: str | None = None) -> ImportResult:
    """导入原始书本并落库 books / volumes / chapters（幂等：INSERT OR IGNORE）。

    幂等语义：同一 book_id 重复导入不产生重复章节（chapters 唯一约束
    (book_id, ordinal)）；内容修订检测由索引层（阶段 03 后半）负责。
    """
    suffix = path.suffix.lower()
    parser = _SUPPORTED.get(suffix)
    if parser is None:
        raise ValueError(f"不支持的格式 {suffix}，支持：{sorted(_SUPPORTED)}")

    parsed: ParsedBook = parser(path)
    bid = book_id or new_uuid_id("book")
    repo = Repository(engine)

    repo.create_book(
        bid,
        parsed.title,
        source_format=parsed.source_format,
        source_path=parsed.source_path,
        normalized_content_hash=parsed.normalized.content_hash,
        normalized_text=parsed.normalized.text,
    )

    vol_id_by_ordinal: dict[int, str] = {}
    for vol in parsed.volumes:
        vid = new_uuid_id("vol")
        repo.create_volume(vid, bid, vol.title, vol.ordinal, grouping_version="v1")
        vol_id_by_ordinal[vol.ordinal] = vid

    for ch in parsed.chapters:
        cid = new_uuid_id("ch")
        repo.create_chapter(
            cid,
            bid,
            ch.ordinal,
            title=ch.title,
            char_start=ch.char_start,
            char_end=ch.char_end,
            content_hash=ch.content_hash,
            volume_id=vol_id_by_ordinal.get(ch.volume_ordinal)
            if ch.volume_ordinal is not None
            else None,
        )

    return ImportResult(
        book_id=bid,
        title=parsed.title,
        source_format=parsed.source_format,
        chapter_count=len(parsed.chapters),
        volume_count=len(parsed.volumes),
    )
