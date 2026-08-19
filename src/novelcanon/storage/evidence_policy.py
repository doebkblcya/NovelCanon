"""证据状态聚合（定版方案 §6）。

聚合规则：仅 unclear → unverified；仅 supports → supported；
supports 与 refutes 并存 → contested；仅 refutes → rejected。
除 supported 外其余状态默认不进入回答（查询层执行）。
"""

from __future__ import annotations

from collections.abc import Iterable

from novelcanon.schemas.types import ClaimStatus, EvidenceStance


def aggregate_claim_status(stances: Iterable[EvidenceStance]) -> ClaimStatus:
    """把一组证据立场聚合为 claim 状态。空集合视为无证据 → unverified。"""
    seen = set(stances)
    if not seen or seen == {EvidenceStance.UNCLEAR}:
        return ClaimStatus.UNVERIFIED
    if seen == {EvidenceStance.SUPPORTS}:
        return ClaimStatus.SUPPORTED
    if seen == {EvidenceStance.REFUTES}:
        return ClaimStatus.REJECTED
    if EvidenceStance.SUPPORTS in seen and EvidenceStance.REFUTES in seen:
        return ClaimStatus.CONTESTED
    # 其余混合（supports+unclear / refutes+unclear）按主导立场
    if EvidenceStance.SUPPORTS in seen:
        return ClaimStatus.SUPPORTED
    if EvidenceStance.REFUTES in seen:
        return ClaimStatus.REJECTED
    return ClaimStatus.UNVERIFIED
