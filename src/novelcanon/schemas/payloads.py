"""类型专属 payload（定版方案 §5）。

公共字段在 ClaimEnvelope；类型专属字段存一对一子表，不塞入单个 JSON 大字段。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from novelcanon.schemas.types import EventLinkType


class RelationPayload(BaseModel):
    """§5.1 关系。relation_type 来自受控本体；原文短语保存在 relation_raw。"""

    from_entity_id: str
    to_entity_id: str
    relation_type: str
    relation_raw: str = ""
    direction: str = "undirected"


class EventPayload(BaseModel):
    """§5.2 事件。参与者只存 event_participants 关联表，不保存副本。"""

    event_type: str
    summary: str
    location_entity_id: str | None = None
    sequence_in_chapter: int = 0
    narrative_weight: float = Field(default=0.5, ge=0.0, le=1.0)


class EventParticipant(BaseModel):
    """event_participants 关联表行。"""

    event_claim_version_id: str
    entity_id: str
    role: str = ""


class StatePayload(BaseModel):
    """§5.4 状态。field 由 state catalog 约束；自由文本只进 raw_value。
    subject_entity_id 为状态主体（查询必需，且必须是已消歧实体），target 可空。"""

    field: str
    value: str | None = None
    raw_value: str | None = None
    subject_entity_id: str
    target_entity_id: str | None = None


class OrgPayload(BaseModel):
    """§5.5 势力事件。成员列表与势力状态由事件日志按查询时间派生。"""

    org_entity_id: str
    member_entity_id: str
    role: str = ""
    action: str = "join"


class ForeshadowPayload(BaseModel):
    """§5.6 伏笔候选：首期只记录线索、原因与关联实体，无状态机。"""

    clue_anchor: str
    related_entity_ids: list[str] = Field(default_factory=list)


class TermDefinitionPayload(BaseModel):
    """术语释义（§4.2 terms）。"""

    term_id: str
    definition: str


class EventLinkPayload(BaseModel):
    """§5.3 事件链接；results 是 causes 的反向查询，不单独存储。"""

    source_event_id: str
    target_event_id: str
    relation_type: EventLinkType
