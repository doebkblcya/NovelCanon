"""CanonicalMemoryV4（定版方案 §4.2）。

全书消歧、事件链接和证据验证完成后生成：entities / claims /
claim_evidence / event_links / unresolved。别名不复制到实体表。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from novelcanon.schemas.draft import UnresolvedMention
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.payloads import (
    EventLinkPayload,
    EventPayload,
    ForeshadowPayload,
    OrgPayload,
    RelationPayload,
    StatePayload,
    TermDefinitionPayload,
)
from novelcanon.schemas.types import EntityTier, EvidenceStance, EvidenceType, Operation

ClaimPayload = (
    RelationPayload
    | EventPayload
    | StatePayload
    | OrgPayload
    | ForeshadowPayload
    | TermDefinitionPayload
)


class EntityRecord(BaseModel):
    """实体最小字段（§4.2）；别名只存 alias claims。"""

    canonical_id: str
    canonical_name: str
    tier: EntityTier = EntityTier.MINOR
    importance_score: float = Field(default=0.0, ge=0.0, le=100.0)
    created_by_run_id: str


class AliasClaim(BaseModel):
    """别名披露记录：canonical_id + surface_name 的事实槽，随披露顺序演进。"""

    alias_fact_id: str
    claim_version_id: str
    canonical_id: str
    surface_name: str
    operation: Operation = Operation.ASSERT
    supersedes_version_id: str | None = None
    observed_ordinal: int | None = None
    observed_chapter_id: str | None = None
    created_by_run_id: str
    created_at: str


class ClaimRecord(BaseModel):
    """envelope + 类型专属 payload。"""

    envelope: ClaimEnvelope
    payload: ClaimPayload


class EvidenceRecord(BaseModel):
    """claim_evidence：source span 唯一持有者（§6）。"""

    evidence_id: str
    claim_version_id: str
    evidence_stance: EvidenceStance
    evidence_type: EvidenceType = EvidenceType.DIRECT
    chapter_id: str
    char_start: int
    char_end: int
    span_hash: str
    literal_match_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    verification_method: str = ""
    verification_run_id: str | None = None


class EventLinkRecord(BaseModel):
    """一等事实表 event_links（§5.3）。"""

    envelope: ClaimEnvelope
    payload: EventLinkPayload


class CanonicalMemoryV4(BaseModel):
    """Canonical 阶段产物。"""

    book_id: str
    run_id: str
    entities: list[EntityRecord] = Field(default_factory=list)
    alias_claims: list[AliasClaim] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    event_links: list[EventLinkRecord] = Field(default_factory=list)
    unresolved: list[UnresolvedMention] = Field(default_factory=list)
