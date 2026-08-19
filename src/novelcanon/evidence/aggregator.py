"""证据聚合器（阶段 07，docs/implementation/07 §4、§5）。

- claim_status 聚合：严格执行四格表（仅 unclear→unverified；仅
  supports→supported；supports+refutes→contested；仅 refutes→rejected）；
- primary evidence 选择：只用于查询加速，取第一条 direct supports
  证据（07 §5）；evidence_id 依赖 claim version_id，由 materialize
  阶段按 primary span 计算；
- 多段证据：一个 claim 可以关联多段 evidence。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from novelcanon.evidence.models import AlignedEvidence
from novelcanon.schemas.types import ClaimStatus, EvidenceStance, EvidenceType
from novelcanon.storage.evidence_policy import aggregate_claim_status


@dataclass(frozen=True)
class AggregatedResult:
    """一个 claim 的证据聚合结果（primary 为选中的 AlignedEvidence）。"""

    claim_status: ClaimStatus
    evidences: list[AlignedEvidence] = field(default_factory=list)
    primary: AlignedEvidence | None = None


class EvidenceAggregator:
    """把一组证据聚合为 claim 状态并选出 primary evidence。"""

    def aggregate(self, evidences: list[AlignedEvidence]) -> AggregatedResult:
        if not evidences:
            return AggregatedResult(claim_status=ClaimStatus.UNVERIFIED)
        status = aggregate_claim_status([e.stance for e in evidences])
        # primary 只用于查询加速（07 §5）：direct supports 优先，
        # 否则取第一条 supports（contextual 也具备 hash 复现能力）
        primary = next(
            (
                e
                for e in evidences
                if e.stance == EvidenceStance.SUPPORTS
                and e.evidence_type == EvidenceType.DIRECT
            ),
            next(
                (e for e in evidences if e.stance == EvidenceStance.SUPPORTS),
                None,
            ),
        )
        return AggregatedResult(
            claim_status=status, evidences=evidences, primary=primary
        )
