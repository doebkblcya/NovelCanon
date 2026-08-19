"""领域枚举：claim 类型、状态、时间语义、证据立场（定版方案 §4–§6）。"""

from __future__ import annotations

from enum import StrEnum


class ClaimType(StrEnum):
    """公共 envelope 的 claim_type 值域（定版方案 §4.2）。"""

    RELATION = "relation"
    EVENT = "event"
    STATE = "state"
    ORG = "org"
    FORESHADOWING = "foreshadowing"
    EVENT_LINK = "event_link"
    TERM_DEFINITION = "term_definition"


class Operation(StrEnum):
    """版本操作：append-only，update/retract 必须指向旧版本（§4.3）。"""

    ASSERT = "assert"
    UPDATE = "update"
    RETRACT = "retract"


class ClaimStatus(StrEnum):
    """证据聚合结果（§6）：仅 unclear→unverified；仅 supports→supported；
    supports+refutes→contested；仅 refutes→rejected。"""

    UNVERIFIED = "unverified"
    SUPPORTED = "supported"
    CONTESTED = "contested"
    REJECTED = "rejected"


class WorldValidKind(StrEnum):
    """世界有效时间类型（§4.4）：story_time / chapter_proxy / unknown。"""

    STORY_TIME = "story_time"
    CHAPTER_PROXY = "chapter_proxy"
    UNKNOWN = "unknown"


class EvidenceStance(StrEnum):
    """证据立场（§6）。"""

    SUPPORTS = "supports"
    REFUTES = "refutes"
    UNCLEAR = "unclear"


class EvidenceType(StrEnum):
    """证据类型（§6）。"""

    DIRECT = "direct"
    CONTEXTUAL = "contextual"
    INFERRED = "inferred"


class EntityTier(StrEnum):
    """实体重要性分层（§4.2），供图谱渲染与查询排序。"""

    CORE = "core"
    MAJOR = "major"
    MINOR = "minor"
    ONE_OFF = "one_off"


class RunStatus(StrEnum):
    """extraction run 状态机（阶段 04，docs/implementation/04）。

    created → running → validating → ready_to_activate → active；
    running/validating → failed / retrying；同书新 run 激活时旧 active → superseded。
    """

    CREATED = "created"
    RUNNING = "running"
    VALIDATING = "validating"
    READY_TO_ACTIVATE = "ready_to_activate"
    ACTIVE = "active"
    FAILED = "failed"
    RETRYING = "retrying"
    SUPERSEDED = "superseded"


class EventLinkType(StrEnum):
    """事件链接关系（§5.3）；results 是 causes 的反向查询，不单独存储。"""

    CAUSES = "causes"
    ENABLES = "enables"
    PREVENTS = "prevents"
