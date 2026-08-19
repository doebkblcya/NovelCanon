"""因果边关系证据验证器（阶段 09 §3/§4，验收 P0 两轮收紧）。

规则层（EventLinker）只生成 candidate；边要 supported 必须有**关系证据**
——目标章原文同一句同时出现「源事件动作/摘要锚点」与「强因果连接词」：

- 源事件动作锚点：原因端 **event_type 标签或 summary 原文**（表达源事件
  动作/内容的表述）出现在目标章文本——**不含参与者**（候选本来就要求
  参与者交集，参与者出现是必然的，不能作为因果证据）；
- 强因果连接词：因此/于是/所以/从而/导致/使得/由此/因而/以致/促使/
  引发/促成/因为/之所以 等——**纯时间推进词（之后/随后/终于）不作为
  因果充分条件**（「甲吃了早饭，随后甲中了彩票」不是因果）。

连接词必须与源事件锚点同句出现（形成同一因果结构），避免连接词指向
目标章内另一件事。只靠参与者交集 + 时间先后（规则候选）不得自动
supported。验证通过时记录验证方法（method）与原文 span（可审计）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 中文句边界（与证据 span 匹配同一口径）
_SENTENCE_BOUNDARY = re.compile(r"[。！？；…\n]+")

# 强因果连接词（目标章内表达「因为/于是/由此」推进的原文表述）。
# 纯时间推进词（之后/随后/终于）不在此列——时间先后是规则候选的
# 前置条件，不能作为因果证据（验收 P0）。
CAUSAL_CONNECTIVES = (
    "因此",
    "于是",
    "所以",
    "从而",
    "导致",
    "使得",
    "由此",
    "这才",
    "因而",
    "以致",
    "促使",
    "引发",
    "促成",
    "因为",
    "之所以",
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
    """确定性因果边验证器：目标章内「源事件动作锚点 + 强连接词」同句命中。"""

    def __init__(self, connectives: tuple[str, ...] = CAUSAL_CONNECTIVES) -> None:
        self._connectives = connectives

    def verify(
        self,
        target_chapter_id: str,
        target_text: str,
        source_refs: list[str],
    ) -> LinkVerification | None:
        """在目标章文本中寻找支持因果边的关系证据。

        source_refs：源事件的**动作/摘要锚点**（event_type 标签 +
        summary，**不含参与者**——参与者共现是候选前置条件，不能作为
        因果证据）。任一句同时包含至少一个源事件锚点与一个强因果连接词
        → 验证通过，返回该句 span；否则 None（保持 unverified）。
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
