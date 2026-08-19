"""因果边关系证据验证器（阶段 09 §3/§4，验收 P0）。

规则层（EventLinker）只生成 candidate；边要 supported 必须有**关系证据**
——目标章原文同时出现「原因事件引用」与「因果连接词」：

- 原因事件引用：原因端参与者 surface（canonical 名）/ 原因端 event_type
  标签之一出现在目标章文本（把两个事件联系起来的表述）；
- 因果连接词：因此/于是/所以/从而/导致/使得/由此/这才/终于/之后/随后/
  得以/促使/引发/促成 等（表达因果推进的原文表述）。

只靠参与者交集 + 时间先后（规则候选）不得自动 supported：
「甲吃早饭」→「甲中彩票」即使同参与者同先后，目标章没有因果表述 →
保持 unverified。

验证通过时记录验证方法（method）与原文 span（可审计，09 §4「保存
prompt/profile 和证据」的确定性实现）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 中文句边界（与证据 span 匹配同一口径）
_SENTENCE_BOUNDARY = re.compile(r"[。！？；…\n]+")

# 因果连接词（目标章内表达「因为/于是/由此」推进的原文表述）
CAUSAL_CONNECTIVES = (
    "因此",
    "于是",
    "所以",
    "从而",
    "导致",
    "使得",
    "由此",
    "这才",
    "终于",
    "之后",
    "随后",
    "得以",
    "促使",
    "引发",
    "促成",
    "正因为",
    "正是因为",
)


@dataclass(frozen=True)
class LinkVerification:
    """一条因果边的关系证据验证结果（含可审计的原文 span）。"""

    method: str
    chapter_id: str
    char_start: int
    char_end: int
    span_text: str
    matched_ref: str
    matched_connective: str


class LinkVerifier:
    """确定性因果边验证器：目标章内「原因引用 + 因果连接词」同句命中。"""

    def __init__(self, connectives: tuple[str, ...] = CAUSAL_CONNECTIVES) -> None:
        self._connectives = connectives

    def verify(
        self,
        target_chapter_id: str,
        target_text: str,
        source_refs: list[str],
    ) -> LinkVerification | None:
        """在目标章文本中寻找支持因果边的关系证据。

        source_refs：原因事件的引用表述（参与者 canonical 名 +
        event_type）。任一句同时包含至少一个引用与一个连接词 → 验证通过，
        返回该句 span；否则 None（保持 unverified）。
        """
        refs = [r for r in source_refs if r and len(r) >= 2]
        if not refs or not target_text:
            return None
        spans = _sentence_spans(target_text) or [(0, len(target_text))]
        for lo, hi in spans:
            sentence = target_text[lo:hi]
            matched_ref = next((r for r in refs if r in sentence), None)
            if matched_ref is None:
                continue
            matched_conn = next(
                (c for c in self._connectives if c in sentence), None
            )
            if matched_conn is None:
                continue
            return LinkVerification(
                method="causal-connective",
                chapter_id=target_chapter_id,
                char_start=lo,
                char_end=hi,
                span_text=sentence.strip(),
                matched_ref=matched_ref,
                matched_connective=matched_conn,
            )
        return None


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """章文本内的句子半开区间（含句尾标点）。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        end = match.end()
        spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    return spans
