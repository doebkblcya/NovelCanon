"""原文导入（阶段 03）：格式适配器（EPUB 优先 / TXT 兜底）→ 统一章节结构。"""

from novelcanon.ingestion.chapter_split import (
    ChapterBoundary,
    ChapterCandidate,
    find_chapter_boundaries,
    find_volume_boundaries,
    split_by_titles,
)
from novelcanon.ingestion.epub import ParsedBook, RawChapter, RawVolume, parse_epub
from novelcanon.ingestion.normalize import (
    NormalizedText,
    normalize_bytes,
    normalize_text,
    sha256,
    slice_by_char_range,
    span_hash,
)
from novelcanon.ingestion.service import ImportResult, import_book
from novelcanon.ingestion.txt import parse_txt

__all__ = [
    "ChapterBoundary",
    "ChapterCandidate",
    "ImportResult",
    "NormalizedText",
    "ParsedBook",
    "RawChapter",
    "RawVolume",
    "find_chapter_boundaries",
    "find_volume_boundaries",
    "import_book",
    "normalize_bytes",
    "normalize_text",
    "parse_epub",
    "parse_txt",
    "sha256",
    "slice_by_char_range",
    "span_hash",
    "split_by_titles",
]
