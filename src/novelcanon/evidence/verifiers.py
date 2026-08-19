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
    """确定性字面验证（07 §3、§6）。

    规则：
    - 候选命中全部锚文本（rate == 1.0）→ supports + direct；
    - 命中部分锚文本（rate >= 0.5）→ supports + contextual（不伪装 direct）；
    - 命中过少（rate < 0.5）→ 低置信，返回 None（由调用方决定是否走
      entailment verifier）。
    """

    MIN_RATE_DIRECT = 1.0
    MIN_RATE_CONTEXTUAL = 0.5

    def verify(self, candidate: SpanCandidate) -> Verification | None:
        rate = candidate.literal_match_rate
        if rate >= self.MIN_RATE_DIRECT:
            return Verification(
                stance=EvidenceStance.SUPPORTS,
                evidence_type=EvidenceType.DIRECT,
                literal_match_rate=rate,
                method="literal-match-direct",
            )
        if rate >= self.MIN_RATE_CONTEXTUAL:
            return Verification(
                stance=EvidenceStance.SUPPORTS,
                evidence_type=EvidenceType.CONTEXTUAL,
                literal_match_rate=rate,
                method="literal-match-contextual",
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
