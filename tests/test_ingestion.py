"""阶段 03：导入与规范化黄金测试（docs/implementation/03 验证项）。"""

from pathlib import Path

from novelcanon.ingestion.epub import parse_epub, verify_chapter_range
from novelcanon.ingestion.normalize import (
    normalize_bytes,
    normalize_text,
    slice_by_char_range,
    span_hash,
)
from novelcanon.ingestion.service import import_book


def test_normalize_idempotent() -> None:
    raw = "第一节\r\n第\u4e00\u8282\u200b组合字符：e\u0301\r\n"
    once = normalize_text(raw)
    twice = normalize_text(once)
    assert once == twice  # 幂等
    assert "\r" not in once


def test_normalize_lf_unified() -> None:
    assert normalize_text("a\r\nb\rc") == "a\nb\nc"


def test_normalize_bytes_hash() -> None:
    norm = normalize_bytes("内容".encode())
    assert norm.content_hash == span_hash("内容")


def test_slice_by_char_range_half_open() -> None:
    text = "甲乙丙丁"
    assert slice_by_char_range(text, 1, 3) == "乙丙"
    try:
        slice_by_char_range(text, 3, 99)
        raise AssertionError("越界应抛 IndexError")
    except IndexError:
        pass


def test_split_by_titles_cover_no_holes() -> None:
    from novelcanon.ingestion.chapter_split import split_by_titles

    text = "第一章 雨夜\n正文一。\n第二章 三年\n正文二。\n第三章 异火\n正文三。"
    cands = split_by_titles(text)
    assert [c.title for c in cands] == ["第一章 雨夜", "第二章 三年", "第三章 异火"]
    # 覆盖全书：候选区间相邻且到结尾
    assert cands[0].char_start == 0
    assert all(a.char_end == b.char_start for a, b in zip(cands, cands[1:], strict=False))
    assert cands[-1].char_end == len(text)


def test_epub_parse_roundtrip(epub_file: Path) -> None:
    book = parse_epub(epub_file)
    assert book.title == "测试小说"
    assert len(book.chapters) == 3
    # 每章 char range 可从全书文本精确切回
    for ch in book.chapters:
        content = verify_chapter_range(book, ch)
        assert content.strip()
        assert ch.title in content
    # 覆盖全书无空洞
    total = sum(c.char_end - c.char_start for c in book.chapters)
    assert total == len(book.normalized.text)


def test_import_book_persists(imported_book) -> None:
    engine, book_id = imported_book
    from novelcanon.storage.repository import Repository

    repo = Repository(engine)
    chapters = repo.list_chapters(book_id)
    assert len(chapters) == 3
    assert chapters[0]["title"].startswith("第一章")
    assert repo.get_book_text(book_id)  # 全书文本已落库


def test_import_idempotent(imported_book) -> None:
    engine, book_id = imported_book
    from novelcanon.storage.repository import Repository

    repo = Repository(engine)
    src = _fixture_path(engine, book_id)
    import_book(engine, src)  # 重复导入同一文件：幂等
    assert len(repo.list_chapters(book_id)) == 3


def _fixture_path(engine, book_id: str) -> Path:
    """取 fixture 文件路径（导入时记录的 source_path）。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT source_path FROM books WHERE book_id = :b"), {"b": book_id}
        ).fetchone()
    assert row is not None
    return Path(row[0])
