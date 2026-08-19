"""请求分段与 ref 映射（阶段 06，docs/implementation/06 §2）。

超过窗口时按稳定句边界把章节正文拆成 segment，每个 segment 独立请求；
压缩关闭（阶段 06 默认）时 ref_source_segment 直接指向原文区间：
- segment_id：稳定（本章内序号），模型在 provisional_claims 中引用；
- char_offset：segment 在整章规范化原文中的起始偏移；
- segment_content_hash：segment 内容的 SHA-256（供阶段 07 回溯校验）。

ref_source_segments 由 pipeline 构造并注入 Draft（模型只引用段 ID，
不自由生成段），保证「ref_source_segment 均落在当前输入对应的原文范围」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from novelcanon.ingestion.normalize import sha256
from novelcanon.retrieval.tokenizer import Tokenizer
from novelcanon.schemas.draft import RefSourceSegment

# 中文句边界（句号/问号/感叹号/省略号/分号/换行）
_SENTENCE_BOUNDARY = re.compile(r"[。！？；…\n]+")


@dataclass(frozen=True)
class SourceSegment:
    """章节正文的一个稳定切片（token 上限内）。"""

    segment_id: str
    char_start: int
    char_end: int
    content: str
    token_count: int


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """按句边界切分，返回各句的 [start, end) 半开区间（含边界符）。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        end = match.end()
        spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def split_for_window(
    text: str,
    tokenizer: Tokenizer,
    max_tokens: int,
    *,
    overlap_chars: int = 0,
) -> list[SourceSegment]:
    """把文本切成不超过 max_tokens 的稳定 segment（句边界优先）。

    单句超限时按字符硬切（该句不可再分）；overlap_chars 为相邻段重叠字符
    （可选，默认 0 保持段间无重叠、可精确拼回原文）。
    返回空列表仅当 text 为空。
    """
    if not text:
        return []
    max_tokens = max(1, max_tokens)
    segments: list[SourceSegment] = []
    seg_index = 0
    cursor = 0

    def push(start: int, end: int) -> None:
        nonlocal seg_index
        content = text[start:end]
        segments.append(
            SourceSegment(
                segment_id=f"seg_{seg_index}",
                char_start=start,
                char_end=end,
                content=content,
                token_count=tokenizer.count(content),
            )
        )
        seg_index += 1

    while cursor < len(text):
        if tokenizer.count(text[cursor:]) <= max_tokens:
            push(cursor, len(text))
            break
        # 在 [cursor, ...) 内贪心找句边界，使累积不超过 max_tokens
        best_end = -1
        for span_start, span_end in _sentence_spans(text[cursor:]):
            if span_start >= len(text) - cursor:
                break
            candidate_end = cursor + span_end
            if tokenizer.count(text[cursor:candidate_end]) <= max_tokens:
                best_end = candidate_end
            else:
                break
        if best_end > cursor:
            push(cursor, best_end)
            cursor = best_end - overlap_chars if overlap_chars > 0 else best_end
            continue
        # 单句超限：按字符硬切到 max_tokens
        hard_end = min(
            len(text),
            cursor + _hard_cut_chars(text[cursor:], tokenizer, max_tokens),
        )
        if hard_end <= cursor:
            hard_end = cursor + 1  # 至少推进 1 字符，防止死循环
        push(cursor, hard_end)
        cursor = hard_end - overlap_chars if overlap_chars > 0 else hard_end
    return segments


def _hard_cut_chars(text: str, tokenizer: Tokenizer, max_tokens: int) -> int:
    """在剩余文本内找到不超过 max_tokens 的最大字符数（线性近似）。"""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tokenizer.count(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return max(1, lo)


def build_ref_segments(
    chapter_id: str, segments: list[SourceSegment]
) -> list[RefSourceSegment]:
    """每段 → RefSourceSegment（压缩关闭时直接指向原文区间）。"""
    return [
        RefSourceSegment(
            segment_id=seg.segment_id,
            char_offset=seg.char_start,
            segment_content_hash=sha256(seg.content),
        )
        for seg in segments
    ]


def ref_segment_prompt_lines(
    chapter_id: str, segments: list[SourceSegment]
) -> list[str]:
    """给模型的「可用原文段」清单（含偏移与内容 hash）。"""
    return [
        f"{seg.segment_id}：章内偏移 [{seg.char_start},{seg.char_end})"
        f"（{seg.token_count} tokens）hash {sha256(seg.content)[:12]}…"
        for seg in segments
    ]
