"""证据验证器（阶段 07，docs/implementation/07 §3、§6）。

两个独立维度：stance（supports / refutes / unclear）与
type（direct / contextual / inferred）。

- LiteralVerifier：确定性字面规则，不调用模型（07 §6「先确定性字面规则」）。
  direct 证据必须 hash 可完整复现（span 切自规范化原文即满足）；
- EntailmentVerifier：可选语义/蕴含验证（低置信情况才调用模型，
  独立 prompt/profile/version，单独计量）。阶段 07 提供接口与
  NullEntailmentVerifier（未配置时返回 None，不产生 inferred 伪装证据）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from novelcanon.evidence.models import SpanCandidate
from novelcanon.schemas.types import EvidenceStance, EvidenceType


@dataclass(frozen=True)
class Verification:
    """一条候选的验证结论（对应 claim_evidence 一行的 stance/type/method）。"""

    stance: EvidenceStance
    evidence_type: EvidenceType
    literal_match_rate: float
    method: str


class LiteralVerifier:
    """确定性字面验证（07 §3、§6，P0 修复）。

    规则（关键收紧）：
    - **硬锚（实体 surface / relation_raw / value / summary / definition /
      clue_anchor）必须全部命中**（hard_match_rate == 1.0）才可能支持
      claim——字面共现（「甲与乙并肩而立」含甲乙）不能判定「甲杀死乙」
      成立；event/term_definition 的谓词表达（summary/definition）与
      relation_raw 一样是硬锚（阶段 11 修正注释与实现不一致：
      span_candidates.py 已把 summary/definition 设为硬锚）；
    - 硬锚全命中 → supports + direct（span 切自原文，hash 复现）；
    - 硬锚未全命中（含部分命中）→ 返回 None（claim 保持 unclear /
      unverified），不产生 contextual supports 伪装证据；
    - 软锚（raw_value 概括句）不参与支持性判定，只影响排序。

    部分匹配不是「弱支持」：它证明原文提到了某些词，但不证明 claim
    成立。低置信语义判定属于 entailment verifier（07 §6）。
    """

    def verify(self, candidate: SpanCandidate) -> Verification | None:
        if candidate.hard_match_rate >= 1.0:
            return Verification(
                stance=EvidenceStance.SUPPORTS,
                evidence_type=EvidenceType.DIRECT,
                literal_match_rate=candidate.literal_match_rate,
                method="literal-match-direct",
            )
        return None


class EntailmentVerifier(Protocol):
    """可选语义/蕴含验证（07 §6：低置信才调用模型）。

    独立 prompt/profile/version；验证调用单独计量，不并入 Map 调用；
    同一 claim/span/version 的验证结果应可缓存复用（调用方负责）。
    """

    def verify(self, candidate: SpanCandidate) -> Verification | None: ...


class NullEntailmentVerifier:
    """未配置模型验证时的兜底：不做推断（不产生 inferred 伪装 direct）。"""

    def verify(self, candidate: SpanCandidate) -> Verification | None:
        return None
