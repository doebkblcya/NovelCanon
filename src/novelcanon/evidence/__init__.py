"""阶段 07：证据对齐（docs/implementation/07）。

证据处理链：ref_source_segment -> 原文候选范围 -> 字面匹配和 span 候选
-> 语义/蕴含验证 -> claim_evidence -> claim_status 聚合。

本包实现：
- ref_mapper：ref 回映射（ref_source_segment -> 原文 span，hash 100% 复现验证）；
- span_candidates：在引用范围内生成 span 候选（字面匹配 + 排序）；
- verifiers：字面验证（确定性）+ 可选蕴含验证（模型，低置信才调用）；
- aggregator：claim_status 聚合 + primary evidence 选择；
- service：编排 staging -> 对齐 -> materialize -> 错误表。
"""

from __future__ import annotations

from novelcanon.evidence.aggregator import EvidenceAggregator
from novelcanon.evidence.models import AlignedEvidence, SpanCandidate
from novelcanon.evidence.ref_mapper import RefMapper, RefMappingError
from novelcanon.evidence.service import EvidenceService
from novelcanon.evidence.span_candidates import SpanCandidateGenerator
from novelcanon.evidence.verifiers import EntailmentVerifier, LiteralVerifier

__all__ = [
    "AlignedEvidence",
    "EntailmentVerifier",
    "EvidenceAggregator",
    "EvidenceService",
    "LiteralVerifier",
    "RefMapper",
    "RefMappingError",
    "SpanCandidate",
    "SpanCandidateGenerator",
]
