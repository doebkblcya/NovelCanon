"""章节边界识别（定版方案 §3.2）。

规则：行首匹配中文卷/章标题模式。EPUB 以 spine 条目为候选，
条目内用标题规则再切分；卷边界来自 ncx 层级或标题规则兜底。
所有 offset 为全书规范化文本的 code point 半开区间。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 卷标题：第X卷 / 卷一 / 上卷 / 中卷 / 下卷
VOLUME_RE = re.compile(r"^\s*(?:第[零一二三四五六七八九十百千万0-9０-９]+[卷部集]|[上下中]卷)\s*$")

# 章/卷标题前缀（容忍「第一卷 陨落的天才」这类带卷名的行；用于目录页判定）
TITLE_PREFIX_RE = re.compile(r"^\s*第[零〇一二两三四五六七八九十百千万0-9０-９]+[章回节卷部集]")

# 章标题：第X章/回/节；以及常见非编号章节
CHAPTER_RE = re.compile(
    r"^\s*("
    r"第[零〇一二两三四五六七八九十百千万0-9０-９]+[章回节]"
    r"|序章|楔子|尾声|终章|番外|后记|前言|引子|结局"
    r")([：:、.．\s]*.*)$"
)

# 章标题行长度上限（防正文长句误判）
_MAX_TITLE_LEN = 40


@dataclass(frozen=True)
class ChapterBoundary:
    """章节标题边界：char_start 为标题行起点。"""

    char_start: int
    title: str


def find_chapter_boundaries(text: str) -> list[ChapterBoundary]:
    """在文本中定位章节标题行（按行扫描，行首匹配）。"""
    out: list[ChapterBoundary] = []
    for m in re.finditer(r"^[^\n]*$", text, re.M):
        line = m.group(0)
        if not line.strip():
            continue
        if len(line.strip()) > _MAX_TITLE_LEN:
            continue
        if CHAPTER_RE.match(line):
            out.append(ChapterBoundary(char_start=m.start(), title=line.strip()))
    return out


def find_volume_boundaries(text: str) -> list[ChapterBoundary]:
    """定位卷标题行（用于标题规则兜底分组）。"""
    out: list[ChapterBoundary] = []
    for m in re.finditer(r"^[^\n]*$", text, re.M):
        line = m.group(0)
        if VOLUME_RE.match(line):
            out.append(ChapterBoundary(char_start=m.start(), title=line.strip()))
    return out


@dataclass(frozen=True)
class ChapterCandidate:
    """章节候选：char range 覆盖 [char_start, char_end)，标题来自规则或元数据。"""

    title: str
    char_start: int
    char_end: int
    ordinal: int

    @property
    def content(self) -> str | None:
        return None  # 由调用方切片；此处仅携带范围

    def __len__(self) -> int:
        return self.char_end - self.char_start


def split_by_titles(text: str, base_ordinal: int = 0) -> list[ChapterCandidate]:
    """按标题规则把文本切为章节候选。

    - 无标题：整段非空文本作为一个无标题章节候选；
    - 有标题：第一个标题前的文本并入第一章（保证章节覆盖全书、无空洞）；
    - 末章延伸到文本结尾。
    """
    bounds = find_chapter_boundaries(text)
    if not bounds:
        if text.strip():
            return [
                ChapterCandidate(
                    title="（无标题）", char_start=0, char_end=len(text), ordinal=base_ordinal
                )
            ]
        return []

    candidates: list[ChapterCandidate] = []
    starts = [b.char_start for b in bounds]
    titles = [b.title for b in bounds]
    for i in range(len(starts)):
        start = 0 if i == 0 else starts[i]  # 前置文本并入首章
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        candidates.append(
            ChapterCandidate(
                title=titles[i], char_start=start, char_end=end, ordinal=base_ordinal + i
            )
        )
    return candidates
