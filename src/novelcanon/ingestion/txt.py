"""TXT importer（阶段 03 兜底格式）。

读取 → 编码探测（utf-8 / gb18030）→ 规范化 → 标题规则全书切分 →
卷标题规则分组。输出与 EPUB 相同的 ParsedBook 结构。
"""

from __future__ import annotations

from pathlib import Path

from novelcanon.ingestion.chapter_split import (
    find_volume_boundaries,
    split_by_titles,
)
from novelcanon.ingestion.epub import ParsedBook, RawChapter, RawVolume
from novelcanon.ingestion.normalize import NormalizedText, normalize_text, sha256

_ENCODINGS = ("utf-8", "gb18030", "big5")


def _decode(raw: bytes) -> str:
    last_error: UnicodeDecodeError | None = None
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"无法解码 TXT（尝试 {_ENCODINGS}）") from last_error


def parse_txt(path: Path) -> ParsedBook:
    """解析 TXT 为 ParsedBook。"""
    raw = path.read_bytes()
    text = normalize_text(_decode(raw))
    full_hash = sha256(text)

    candidates = split_by_titles(text)
    # 卷分组：卷标题行位置把章候选分段
    vol_bounds = find_volume_boundaries(text)
    vol_titles: list[tuple[int, str]] = [(b.char_start, b.title) for b in vol_bounds]

    chapters: list[RawChapter] = []
    for i, cand in enumerate(candidates):
        vol_title: str | None = None
        vol_idx: int | None = None
        for idx, (v_start, v_title) in enumerate(vol_titles):
            if v_start <= cand.char_start:
                vol_title, vol_idx = v_title, idx
        chapters.append(
            RawChapter(
                title=cand.title,
                char_start=cand.char_start,
                char_end=cand.char_end,
                content_hash=sha256(text[cand.char_start : cand.char_end]),
                ordinal=i,
                volume_title=vol_title,
                volume_ordinal=(vol_idx + 1) if vol_idx is not None else None,
            )
        )

    volumes: list[RawVolume] = []
    vol_groups: dict[int, list[int]] = {}
    for c in chapters:
        if c.volume_ordinal is not None:
            vol_groups.setdefault(c.volume_ordinal, []).append(c.ordinal)
    for vol_ord, ch_ords in sorted(vol_groups.items()):
        title = next(
            (c.volume_title for c in chapters if c.volume_ordinal == vol_ord), f"第{vol_ord}卷"
        )
        volumes.append(
            RawVolume(
                title=title or f"第{vol_ord}卷",
                ordinal=vol_ord - 1,
                chapter_ordinals=tuple(ch_ords),
            )
        )

    return ParsedBook(
        title=path.stem,
        source_format="txt",
        source_path=str(path),
        normalized=NormalizedText(text=text, content_hash=full_hash),
        chapters=chapters,
        volumes=volumes,
    )
