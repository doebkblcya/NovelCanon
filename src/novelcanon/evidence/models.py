"""阶段 07：证据对齐的数据结构（docs/implementation/07）。

AlignedEvidence 是对齐完成的证据（可直接落 claim_evidence）；
SpanCandidate 是字面匹配生成的候选（待验证/排序）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from novelcanon.schemas.types import EvidenceStance, EvidenceType


@dataclass(frozen=True)
class SpanCandidate:
    """引用范围内的一段原文候选（span 候选，07 §2）。

    候选必须切自规范化原文（chapter_text[char_start:char_end]），
    hash 可 100% 复现；排序按 literal_match_rate / 距离 / 上下文。

    hard_match_rate：硬锚（实体 surface / relation_raw / value 等 claim
    内容词）命中率——只有硬锚全部命中的候选才可能支持 claim（P0 修复：
    字面共现不能直接判定事实成立）。
    """

    chapter_id: str
    char_start: int
    char_end: int
    span_text: str
    literal_match_rate: float = 0.0
    hard_match_rate: float = 0.0
    score: float = 0.0
    matched_anchors: list[str] = field(default_factory=list)
    total_anchors: int = 0


@dataclass(frozen=True)
class AlignedEvidence:
    """一条已完成对齐的证据（可写入 claim_evidence，07 §3）。

    - stance / type 双维度：direct 必须 hash 可完整复现；
    - literal_match_rate 与 verification_method 记录候选选择过程（07 §2 验证方法）。
    """

    chapter_id: str
    char_start: int
    char_end: int
    span_text: str
    stance: EvidenceStance = EvidenceStance.SUPPORTS
    evidence_type: EvidenceType = EvidenceType.DIRECT
    literal_match_rate: float = 1.0
    verification_method: str = "literal-match"
