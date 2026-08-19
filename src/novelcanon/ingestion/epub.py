"""EPUB importer（阶段 03 优先格式，ADR 决策）。

解析链路：zip → META-INF/container.xml 定位 OPF → manifest/spine 顺序 →
逐条目 HTML 文本提取 → 全书规范化拼接 → ncx 提供卷/章层级元数据 →
标题规则兜底切分。输出与 TXT 相同的 ParsedBook 结构。
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from novelcanon.ingestion.chapter_split import (
    TITLE_PREFIX_RE,
    VOLUME_RE,
    split_by_titles,
)
from novelcanon.ingestion.normalize import NormalizedText, normalize_text, sha256

_BLOCK_TAGS = {
    "p",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "br",
    "li",
    "tr",
    "blockquote",
    "section",
    "article",
}
_SKIP_TAGS = {"script", "style", "head", "title"}
_EMPTY_ENTRY_LEN = 10  # 空条目（封面/图片章）阈值


def _local(tag: str) -> str:
    """去掉 XML 命名空间取局部名。"""
    return tag.rsplit("}", 1)[-1]


class _TextExtractor(HTMLParser):
    """XHTML → 纯文本：块级边界插入换行，跳过 script/style。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def extract_html_text(raw: bytes) -> str:
    """HTML/XHTML 字节 → 粗文本（尚未规范化）。"""
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        html = raw.decode("utf-8", errors="replace")
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


@dataclass(frozen=True)
class RawChapter:
    """导入层章节：范围基于**全书规范化文本**。"""

    title: str
    char_start: int
    char_end: int
    content_hash: str
    ordinal: int
    volume_title: str | None = None
    volume_ordinal: int | None = None


@dataclass(frozen=True)
class RawVolume:
    """导入层卷：记录卷标题、顺序与包含的章 ordinal。"""

    title: str
    ordinal: int
    chapter_ordinals: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ParsedBook:
    """导入产物：全书规范化文本 + 章节 + 卷（与格式无关）。"""

    title: str
    source_format: str
    source_path: str
    normalized: NormalizedText
    chapters: list[RawChapter]
    volumes: list[RawVolume]


# ---------------------------------------------------------------------------
# EPUB 内部结构
# ---------------------------------------------------------------------------


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    try:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
    except KeyError as exc:
        raise ValueError("EPUB 缺少 META-INF/container.xml") from exc
    for rootfile in container.iter():
        if _local(rootfile.tag) == "rootfile":
            path = rootfile.attrib.get("full-path")
            if path:
                return path
    # 兜底：扫描 *.opf
    for name in zf.namelist():
        if name.endswith(".opf"):
            return name
    raise ValueError("EPUB 中找不到 OPF 文件")


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> tuple[str, dict[str, str], list[str]]:
    root = ET.fromstring(zf.read(opf_path))
    manifest: dict[str, str] = {}
    spine: list[str] = []
    title = opf_path

    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "item":
            item_id = elem.attrib.get("id")
            href = elem.attrib.get("href")
            if item_id and href:
                manifest[item_id] = href
        elif tag == "itemref":
            idref = elem.attrib.get("idref")
            if idref:
                spine.append(idref)
        elif tag == "title" and title == opf_path:
            if elem.text and elem.text.strip():
                title = elem.text.strip()
    return title, manifest, spine


def _find_ncx(zf: zipfile.ZipFile) -> str | None:
    for name in zf.namelist():
        base = name.rsplit("/", 1)[-1]
        if base.endswith(".ncx"):
            return name
    return None


@dataclass(frozen=True)
class _NcxNode:
    title: str
    src: str | None
    level: int
    children: tuple[_NcxNode, ...] = ()


def _parse_ncx(zf: zipfile.ZipFile, ncx_path: str) -> _NcxNode | None:
    root = ET.fromstring(zf.read(ncx_path))

    def parse_node(elem: ET.Element, level: int) -> _NcxNode:
        title = ""
        src: str | None = None
        children: list[_NcxNode] = []
        for child in elem:
            tag = _local(child.tag)
            if tag == "navLabel":
                for t in child.iter():
                    if _local(t.tag) == "text" and t.text:
                        title = t.text.strip()
            elif tag == "content":
                src = child.attrib.get("src")
            elif tag == "navPoint":
                children.append(parse_node(child, level + 1))
        return _NcxNode(title=title, src=src, level=level, children=tuple(children))

    for nav_map in root.iter():
        if _local(nav_map.tag) == "navMap":
            points = [parse_node(p, 1) for p in nav_map if _local(p.tag) == "navPoint"]
            if points:
                return _NcxNode(title="root", src=None, level=0, children=tuple(points))
    return None


@dataclass(frozen=True)
class _ChapterMeta:
    title: str | None
    volume_title: str | None
    volume_ordinal: int | None


def _ncx_metadata(node: _NcxNode) -> dict[str, _ChapterMeta]:
    """把 ncx 树映射为 {src basename → 章元数据（含卷归属）}。

    规则：第 1 层节点**有子节点**时视为卷（其下叶为章，卷标题取该层标题）；
    第 1 层节点无子（平铺章）时无卷。
    """
    out: dict[str, _ChapterMeta] = {}

    def walk(n: _NcxNode, volume_title: str | None, volume_ordinal: int | None) -> None:
        if not n.children:
            if n.src:
                key = _basename(n.src) or n.src
                # 只有看起来像「第X卷/上中下卷」的才是卷标题；
                # 「目录/正文」等伪分组不设卷（阶段 10 按 50 章兜底，§10）。
                if volume_title and not VOLUME_RE.match(volume_title):
                    volume_title = None
                    volume_ordinal = None
                out[key] = _ChapterMeta(
                    title=n.title or None,
                    volume_title=volume_title,
                    volume_ordinal=volume_ordinal,
                )
            return
        for i, child in enumerate(n.children, start=1):
            if n.level == 1:
                walk(child, n.title, i)
            else:
                walk(child, volume_title, volume_ordinal)

    for child in node.children:
        if child.children:  # 第 1 层有子 → 卷
            walk(child, child.title, 1)
        else:  # 平铺章
            walk(child, None, None)
    return out


def _basename(href: str | None) -> str | None:
    if not href:
        return None
    return href.split("#", 1)[0].rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def parse_epub(path: Path) -> ParsedBook:
    """解析 EPUB 为 ParsedBook（chapter/volume 范围基于全书规范化文本）。"""
    with zipfile.ZipFile(path) as zf:
        opf_path = _find_opf_path(zf)
        title, manifest, spine = _parse_opf(zf, opf_path)
        base_dir = posixpath.dirname(opf_path)
        ncx_path = _find_ncx(zf)
        ncx_meta: dict[str, _ChapterMeta] = {}
        if ncx_path:
            node = _parse_ncx(zf, ncx_path)
            if node:
                ncx_meta = _ncx_metadata(node)

        # 1. 逐条目提取并规范化文本（条目间以空行分隔）
        entry_texts: list[str] = []
        entry_hrefs: list[str] = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            full_href = posixpath.join(base_dir, href) if not href.startswith(base_dir) else href
            try:
                raw = zf.read(full_href)
            except KeyError:
                continue
            entry_text = normalize_text(extract_html_text(raw))
            if len(entry_text.strip()) < _EMPTY_ENTRY_LEN:
                continue  # 封面/图片等空条目
            entry_texts.append(entry_text)
            entry_hrefs.append(href)

        # 2. 全书规范化文本（拼接后整体再规范化一次，保证幂等）
        full_text = normalize_text("\n\n".join(entry_texts))
        full_hash = sha256(full_text)

        # 3. 章节组装：条目区间内按标题规则切分，ncx 元数据交叉校验
        chapters: list[RawChapter] = []
        offset = 0
        for i, entry_text in enumerate(entry_texts):
            entry_start = offset
            entry_len = len(entry_text)
            entry_end = entry_start + entry_len
            # 条目间以 "\n\n" 分隔；末条目之后无分隔
            offset = entry_end + (2 if i < len(entry_texts) - 1 else 0)
            candidates = split_by_titles(entry_text, base_ordinal=0)
            meta = ncx_meta.get(_basename(entry_hrefs[i]) or "")
            if _is_toc_entry(entry_text):
                # 整个条目是目录页：作为独立「（目录）」章，正文章保持干净
                chapters.append(
                    RawChapter(
                        title="（目录）",
                        char_start=entry_start,
                        char_end=offset,
                        content_hash=sha256(full_text[entry_start:offset]),
                        ordinal=0,  # 下面统一赋 ordinal
                    )
                )
                continue
            for j, cand in enumerate(candidates):
                ch_title = cand.title
                if meta and meta.title and "（无标题）" in ch_title:
                    ch_title = meta.title
                char_start = entry_start + cand.char_start
                char_end = entry_start + cand.char_end
                # 条目最后一个候选延伸到条目分隔符，保证章节覆盖全书无空洞
                if j == len(candidates) - 1 and i < len(entry_texts) - 1:
                    char_end = offset
                if char_end - char_start < _EMPTY_ENTRY_LEN and ch_title == "（无标题）":
                    # 极短无标题段：并入前一章（保持全书覆盖无空洞）
                    if chapters:
                        prev = chapters[-1]
                        chapters[-1] = RawChapter(
                            title=prev.title,
                            char_start=prev.char_start,
                            char_end=char_end,
                            content_hash=prev.content_hash,
                            ordinal=prev.ordinal,
                            volume_title=prev.volume_title,
                            volume_ordinal=prev.volume_ordinal,
                        )
                    continue
                chapters.append(
                    RawChapter(
                        title=ch_title,
                        char_start=char_start,
                        char_end=char_end,
                        content_hash=sha256(full_text[char_start:char_end]),
                        ordinal=0,  # 下面统一赋 ordinal
                        volume_title=meta.volume_title if meta else None,
                        volume_ordinal=meta.volume_ordinal if meta else None,
                    )
                )

        # 4. 清理目录行 + 统一 ordinal + 卷分组
        chapters = [c for c in chapters if c.char_end > c.char_start]
        chapters = _postprocess_toc_lines(chapters, full_text)
        for idx, c in enumerate(chapters):
            chapters[idx] = RawChapter(
                title=c.title,
                char_start=c.char_start,
                char_end=c.char_end,
                content_hash=c.content_hash,
                ordinal=idx,
                volume_title=c.volume_title,
                volume_ordinal=c.volume_ordinal,
            )

        volumes: list[RawVolume] = []
        vol_by_ordinal: dict[int, list[int]] = {}
        for c in chapters:
            if c.volume_ordinal is not None:
                vol_by_ordinal.setdefault(c.volume_ordinal, []).append(c.ordinal)
        for vol_ord, ch_ordinals in sorted(vol_by_ordinal.items()):
            vol_title = next(
                (c.volume_title for c in chapters if c.volume_ordinal == vol_ord), f"第{vol_ord}卷"
            )
            volumes.append(
                RawVolume(
                    title=vol_title or f"第{vol_ord}卷",
                    ordinal=vol_ord - 1,
                    chapter_ordinals=tuple(ch_ordinals),
                )
            )

        return ParsedBook(
            title=title,
            source_format="epub",
            source_path=str(path),
            normalized=NormalizedText(text=full_text, content_hash=full_hash),
            chapters=chapters,
            volumes=volumes,
        )


def verify_chapter_range(book: ParsedBook, chapter: RawChapter) -> str:
    """验收辅助：从全书规范化文本按 char range 精确切回章节内容。"""
    return book.normalized.text[chapter.char_start : chapter.char_end]


# 行内章标题前缀（无行首锚定，用于紧凑单行目录识别）
_INLINE_TITLE_RE = re.compile(r"第[零〇一二两三四五六七八九十百千万0-9０-９]+[章回节卷部集]")


def _is_title_list(text: str) -> bool:
    """紧凑标题列表判定：≥8 个行内章标题前缀且标题间平均跨度 < 100 字符。

    覆盖 EPUB 单行/紧凑目录（所有章标题以空白分隔成一行，splitlines
    只有 1 行，行级判定失效）；真章正文不会成片出现 8 个以上「第X章」。
    """
    matches = list(_INLINE_TITLE_RE.finditer(text))
    if len(matches) < 8:
        return False
    avg_gap = (matches[-1].start() - matches[0].start()) / (len(matches) - 1)
    return avg_gap < 100


def _is_toc_entry(entry_text: str) -> bool:
    """条目级目录页判定。

    多行目录：≥5 个非空行且 ≥90% 是章/卷标题或导航词；
    单行/紧凑目录（EPUB 常见）：行内标题列表判定。
    目录页整体作为一章（标题「（目录）」），避免目录行污染正文章。
    """
    lines = [line.strip() for line in entry_text.splitlines() if line.strip()]
    if len(lines) >= 5:
        nav_words = {"目录", "正文", "楔子", "序章", "卷首语", "前言", "后记", "尾页"}
        bad = [line for line in lines if not (TITLE_PREFIX_RE.match(line) or line in nav_words)]
        if len(bad) / len(lines) < 0.1:
            return True
    return _is_title_list(entry_text)


def _postprocess_toc_lines(chapters: list[RawChapter], full_text: str) -> list[RawChapter]:
    """清理目录行章：内容仅一个短标题行（<100 字符）的候选视为目录条目，
    丢弃但把其区间并入下一个正常章，保持章节覆盖全书无空洞。"""
    cleaned: list[RawChapter] = []
    pending_start: int | None = None

    def is_toc_line(c: RawChapter) -> bool:
        """目录页/目录行章：绝大多数非空行是章标题或导航词。

        容忍少量排版噪音（如「第八十七 下杀手」「VIP卷」）：
        目录行占比 ≥ 90% 即判定为目录；真章正文不会成片命中标题前缀。
        """
        if c.char_end - c.char_start >= 2000:
            return False
        content = full_text[c.char_start : c.char_end]
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return False
        nav_words = {"目录", "正文", "楔子", "序章", "卷首语", "前言", "后记", "尾页"}
        bad = [line for line in lines if not (TITLE_PREFIX_RE.match(line) or line in nav_words)]
        return len(bad) / len(lines) < 0.1

    for c in chapters:
        if is_toc_line(c):
            if pending_start is None:
                pending_start = c.char_start
            continue
        if pending_start is not None:
            c = RawChapter(
                title=c.title,
                char_start=pending_start,
                char_end=c.char_end,
                content_hash=sha256(full_text[pending_start : c.char_end]),
                ordinal=c.ordinal,
                volume_title=c.volume_title,
                volume_ordinal=c.volume_ordinal,
            )
            pending_start = None
        cleaned.append(c)

    # 尾部残留目录区间：并入最后一章
    if pending_start is not None and cleaned:
        last = cleaned[-1]
        cleaned[-1] = RawChapter(
            title=last.title,
            char_start=last.char_start,
            char_end=len(full_text),
            content_hash=sha256(full_text[last.char_start : len(full_text)]),
            ordinal=last.ordinal,
            volume_title=last.volume_title,
            volume_ordinal=last.volume_ordinal,
        )
    return cleaned
