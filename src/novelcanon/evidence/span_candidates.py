"""span 候选生成器（阶段 07，docs/implementation/07 §2）。

在引用范围（ref 段）内为 claim 生成证据 span 候选：

- 锚文本提取：claim 引用到的 mention surface + payload 中的 raw/值字段
  （relation_raw / summary / value / clue_anchor 等）作为字面匹配依据；
- 标点/空白容差（§2 明确要求）：匹配在「规范化文本」（去标点/空白）
  上进行，命中位置映射回原文区间；最终 span 仍切自规范化原文
  （hash 100% 复现）；
- 候选生成：在段范围内定位每个锚文本的所有出现位置，组合成候选 span；
- 排序：按 literal_match_rate（命中的锚文本占比）优先，其次按
  区间紧凑度（span 越短越优）与上下文（句子边界内优先）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from novelcanon.evidence.models import SpanCandidate

# 中文句边界（与 generation/segments.py 一致）
_SENTENCE_BOUNDARY = re.compile(r"[。！？；…\n]+")

# 跨句合并候选的 span 上限（字符）：防止实体分散导致的超宽区间
_MAX_SPAN_CHARS = 150

# 需要容忍的标点/空白（规范化时删除）：中文标点 + 半角标点 + 空白
_PUNCTUATION = re.compile(r"[\s,，。！？；：、．.·…—–-”“\"'‘’()（）\[\]【】<>《》]+")


@dataclass(frozen=True)
class AnchorTerm:
    """一条锚文本：surface 与来源（mention surface 或 payload 原文字段）。

    hard=True 为「硬锚」：claim 内容词（实体 surface / relation_raw /
    value / raw_value / clue_anchor），必须全部在原文命中才能支持 claim；
    hard=False 为「软锚」：模型概括句（summary / definition），
    原文不逐字出现，只影响候选排序，不决定支持性。
    """

    text: str
    source: str  # "mention:xxx" / "relation_raw" / "summary" / "value" / ...
    hard: bool = True


def _normalize(text: str) -> str:
    """删除标点/空白（全半角统一），返回规范化字符串。"""
    return _PUNCTUATION.sub("", text)


def extract_anchors(
    claim: dict,
    mentions: dict[str, str],
    local_events: list[dict] | None = None,
) -> list[AnchorTerm]:
    """从 claim 提取锚文本（07 §2：字面匹配的搜索词）。

    - mention_id 引用（subject_entity_id / from_entity_id / to_entity_id /
      org_entity_id / member_entity_id / related_entity_ids / participants）
      解析为 surface（硬锚）；
    - event claim：从 local_events 按 sequence/event_type 匹配，补充
      participants 的 surface（硬锚）；
    - payload 原文字段：relation_raw/value/raw_value/clue_anchor 为硬锚，
      summary/definition 为软锚（概括句不逐字出现，不决定支持性）。
    """
    anchors: list[AnchorTerm] = []
    seen: set[str] = set()

    def add(text: str, source: str, hard: bool = True) -> None:
        text = (text or "").strip()
        if text and text not in seen:
            seen.add(text)
            anchors.append(AnchorTerm(text=text, source=source, hard=hard))

    payload = claim.get("payload") or {}
    ctype = claim.get("claim_type", "")
    # mention_id → surface（硬锚）
    for field in (
        "subject_entity_id",
        "from_entity_id",
        "to_entity_id",
        "org_entity_id",
        "member_entity_id",
        "location_entity_id",
    ):
        mid = payload.get(field)
        if isinstance(mid, str) and mid in mentions:
            add(mentions[mid], f"mention:{field}")
    for field in ("related_entity_ids", "participants"):
        ids = payload.get(field)
        if isinstance(ids, list):
            for mid in ids:
                if isinstance(mid, str) and mid in mentions:
                    add(mentions[mid], f"mention:{field}")

    # event：从 local_events 按 event_type + sequence 匹配，补充 participants
    if ctype == "event" and local_events:
        seq = payload.get("sequence_in_chapter")
        etype = payload.get("event_type")
        for ev in local_events:
            if ev.get("event_type") != etype:
                continue
            if seq is not None and ev.get("sequence_in_chapter") not in (None, seq):
                continue
            for mid in ev.get("participants", []):
                if isinstance(mid, str) and mid in mentions:
                    add(mentions[mid], f"event_participant:{mid}")
            break

    # payload 原文字段（硬/软按字段区分）
    for field, hard in (
        ("relation_raw", True),
        ("value", True),
        ("clue_anchor", True),
        ("summary", False),
        ("definition", False),
        ("raw_value", False),
    ):
        raw = payload.get(field)
        if isinstance(raw, str) and len(raw) >= 2:  # 过短短语不参与锚定
            add(raw, field, hard=hard)
    return anchors


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """段文本内的句子半开区间（含句尾标点）。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        end = match.end()
        spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _find_with_tolerance(
    text: str, needle: str
) -> list[tuple[int, int]]:
    """在 text 中查找 needle，容忍标点/空白差异（§2）。

    规范化（去标点/空白）后在规范化文本上匹配，命中位置映射回原文
    区间（半开）。返回原文中的匹配区间列表。
    """
    norm_text = _normalize(text)
    norm_needle = _normalize(needle)
    if not norm_needle:
        return []
    # 规范化字符 → 原文区间映射：累加原始长度（含被删标点）
    # 简化：逐字符扫描，跳过被删字符；对每个规范化字符记录其在原文的索引
    orig_idx: list[int] = []  # norm_text[i] 对应 text 的起始索引
    j = 0
    for i, ch in enumerate(text):
        if _PUNCTUATION.match(ch):
            continue
        orig_idx.append(i)
        j += 1
    positions: list[tuple[int, int]] = []
    start = 0
    while True:
        pos = norm_text.find(norm_needle, start)
        if pos < 0:
            break
        raw_start = orig_idx[pos]
        raw_end = orig_idx[pos + len(norm_needle) - 1] + 1
        positions.append((raw_start, raw_end))
        start = pos + 1
    return positions


class SpanCandidateGenerator:
    """在 ref 段范围内生成并排序证据 span 候选。"""

    def generate(
        self,
        chapter_id: str,
        segment_start: int,
        segment_text: str,
        anchors: list[AnchorTerm],
    ) -> list[SpanCandidate]:
        """返回按 (match_rate desc, span 长度 asc) 排序的候选。

        无锚文本可匹配时返回空列表（调用方决定是否走 entailment）。
        """
        if not anchors or not segment_text:
            return []
        # 1) 每个锚文本在段内的所有出现位置（标点容差匹配）
        hits: dict[int, list[tuple[int, int]]] = {}  # anchor index -> 出现区间
        for idx, anchor in enumerate(anchors):
            positions = _find_with_tolerance(segment_text, anchor.text)
            if positions:
                hits[idx] = positions

        # 2) 双模式生成候选：
        #    a) 句内候选：以句子为单元，覆盖该句内所有锚文本（保底，防超宽）；
        #    b) 跨句合并候选：以单个锚文本命中为种子，贪心并入最近的其他命中
        #       （rate 优先），合并后 span 不超过 _MAX_SPAN_CHARS。
        #    统一排序：match_rate 高优先、span 短优先。
        candidates: list[SpanCandidate] = []
        sentences = _sentence_spans(segment_text) or [(0, len(segment_text))]

        def make_candidate(
            covered: set[int], lo: int, hi: int
        ) -> SpanCandidate | None:
            if not covered:
                return None
            hard_total = sum(1 for a in anchors if a.hard)
            hard_covered = sum(
                1 for i in covered if i < len(anchors) and anchors[i].hard
            )
            rate = len(covered) / len(anchors)
            hard_rate = hard_covered / hard_total if hard_total else 1.0
            span_text = segment_text[lo:hi]
            return SpanCandidate(
                chapter_id=chapter_id,
                char_start=segment_start + lo,
                char_end=segment_start + hi,
                span_text=span_text,
                literal_match_rate=rate,
                hard_match_rate=hard_rate,
                # 排序：硬锚命中率优先，其次总命中率，其次 span 短
                score=hard_rate * 1000 + rate * 100 - (hi - lo),
                matched_anchors=[anchors[i].text for i in sorted(covered)],
                total_anchors=len(anchors),
            )

        # a) 句内候选
        for s_start, s_end in sentences:
            covered: set[int] = set()
            lo, hi = s_start, s_end
            for idx, positions in hits.items():
                for pos_start, pos_end in positions:
                    if s_start <= pos_start and pos_end <= s_end:
                        covered.add(idx)
                        lo = min(lo, pos_start)
                        hi = max(hi, pos_end)
            candidate = make_candidate(covered, lo, hi)
            if candidate is not None:
                candidates.append(candidate)

        # b) 跨句合并候选（span 上限内）
        for anchor_idx, positions in hits.items():
            for pos_start, pos_end in positions:
                covered = {anchor_idx}
                lo, hi = pos_start, pos_end
                for other, other_positions in hits.items():
                    if other in covered:
                        continue
                    nearest = min(
                        other_positions,
                        key=lambda p: max(p[0] - hi, lo - p[1], 0),
                    )
                    nlo, nhi = min(lo, nearest[0]), max(hi, nearest[1])
                    if (nhi - nlo) <= _MAX_SPAN_CHARS:
                        lo, hi = nlo, nhi
                        covered.add(other)
                candidate = make_candidate(covered, lo, hi)
                if candidate is not None:
                    candidates.append(candidate)

        # 3) 排序：match_rate 高优先、span 短优先（score 编码两者）
        unique: dict[tuple[int, int], SpanCandidate] = {}
        for c in candidates:
            key = (c.char_start, c.char_end)
            if key not in unique or c.score > unique[key].score:
                unique[key] = c
        return sorted(unique.values(), key=lambda c: c.score, reverse=True)
