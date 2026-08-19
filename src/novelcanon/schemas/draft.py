"""ExtractionDraftV1（定版方案 §4.1）。

Map 按章并发执行，仅输出本章可确定的信息：无 canonical_id、
无最终证据坐标、无跨章事件 ID；引用仅限同章 local_event_id。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from novelcanon.schemas.payloads import (
    EventPayload,
    ForeshadowPayload,
    OrgPayload,
    RelationPayload,
    StatePayload,
    TermDefinitionPayload,
)
from novelcanon.schemas.types import ClaimType, Operation

ClaimPayload = (
    RelationPayload
    | EventPayload
    | StatePayload
    | OrgPayload
    | ForeshadowPayload
    | TermDefinitionPayload
)


class RefSourceSegment(BaseModel):
    """压缩段到原文范围的映射（§4.1）：压缩段 ID + 段内偏移 + 段内容 hash。"""

    segment_id: str
    char_offset: int
    segment_content_hash: str


class LocalCause(BaseModel):
    """仅引用同章 local_event_id。"""

    local_event_id: str


class CauseCandidate(BaseModel):
    """跨章原因的文本描述和候选实体。"""

    text: str
    candidate_entity_ids: list[str] = Field(default_factory=list)


class UnresolvedMention(BaseModel):
    """无法消歧的实体提及：落库保留并统计，不参与默认查询（§4.1）。"""

    surface_name: str
    chapter_id: str
    char_start: int
    char_end: int
    context: str = ""


class MentionDraft(BaseModel):
    """本章实体提及。"""

    mention_id: str
    surface_name: str
    char_start: int
    char_end: int


class LocalEventDraft(BaseModel):
    """本章事件。participants 引用章内 mention_id。"""

    local_event_id: str
    event_type: str
    summary: str
    participants: list[str] = Field(default_factory=list)


class ProvisionalClaim(BaseModel):
    """临时事实：claim_type + 类型专属 payload，draft 阶段无版本链。"""

    provisional_claim_id: str
    claim_type: ClaimType
    operation: Operation = Operation.ASSERT
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    ref_source_segment_id: str | None = None
    payload: ClaimPayload


class ExtractionDraftV1(BaseModel):
    """单章 Map 输出（§4.1）。"""

    book_id: str
    chapter_id: str
    chapter_ordinal: int
    mentions: list[MentionDraft] = Field(default_factory=list)
    local_events: list[LocalEventDraft] = Field(default_factory=list)
    provisional_claims: list[ProvisionalClaim] = Field(default_factory=list)
    ref_source_segments: list[RefSourceSegment] = Field(default_factory=list)
    local_causes: list[LocalCause] = Field(default_factory=list)
    cause_candidates: list[CauseCandidate] = Field(default_factory=list)
    unresolved: list[UnresolvedMention] = Field(default_factory=list)
