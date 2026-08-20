"""稳定 ID 生成（定版方案 §4.3）。

- chapter_id / canonical_id / run_id：UUID（稳定、持久化、跨 run 不变）；
- fact_id：按 claim 类型取语义字段生成，表示可连续修订的事实槽；
- claim_version_id：fact_id + payload_hash + schema_version；
- evidence_id：claim version + 稳定 source span。

集中实现，禁止各模块自行拼接 ID。payload 语义字段变化 → 新 fact；
payload 内容变化 → 同 fact 新 version；run 信息绝不进入任何 ID。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from pydantic import BaseModel

from novelcanon.config.hash import stable_config_hash
from novelcanon.schemas.types import EventLinkType

_HASH_LEN = 16
SCHEMA_VERSION = "v1"


def _digest(parts: Sequence[str]) -> str:
    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_HASH_LEN]


def _semantic_fact_id(claim_type: str, **fields: object) -> str:
    """对语义字段做规范化排序后取 hash，保证字段顺序无关。"""
    normalized: dict[str, object] = {k: v for k, v in fields.items() if v is not None}
    payload = stable_config_hash(normalized)
    return f"fact_{_digest([claim_type, payload])}"


def new_uuid_id(prefix: str) -> str:
    """UUID 型稳定 ID：chapter_id / canonical_id / run_id / mention_id 等。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def payload_hash(payload: BaseModel | dict[str, object]) -> str:
    """事实内容 hash（不含 observed/run 元数据）。"""
    if isinstance(payload, dict):
        return stable_config_hash(payload)
    return stable_config_hash(payload.model_dump(mode="json"))


def claim_version_id(
    fact_id: str, payload_hash_value: str, schema_version: str = SCHEMA_VERSION
) -> str:
    """claim_version_id = fact_id + payload_hash + schema_version；不含 run。"""
    return f"ver_{_digest([fact_id, payload_hash_value, schema_version])}"


# ---------------------------------------------------------------------------
# fact_id（§4.3：每种 claim 类型明确"哪些字段进入 fact_id"）
# ---------------------------------------------------------------------------


def relation_fact_id(
    from_entity_id: str, relation_type: str, to_entity_id: str, context: str = ""
) -> str:
    """主体、关系类型、客体、关系上下文。"""
    return _semantic_fact_id(
        "relation",
        from_entity_id=from_entity_id,
        relation_type=relation_type,
        to_entity_id=to_entity_id,
        context=context,
    )


def event_fact_id(
    event_type: str,
    participants: Sequence[str],
    location_entity_id: str | None,
    chapter_id: str,
    sequence_in_chapter: int,
) -> str:
    """事件类型、参与者（排序后）、地点、章节锚点、章内序号。"""
    return _semantic_fact_id(
        "event",
        event_type=event_type,
        participants=sorted(set(participants)),
        location_entity_id=location_entity_id,
        chapter_id=chapter_id,
        sequence_in_chapter=sequence_in_chapter,
    )


def state_fact_id(subject_entity_id: str, field: str, target_entity_id: str | None = None) -> str:
    """主体、状态字段、目标；不包含 value（值变化 → 新版本，非新事实）。"""
    return _semantic_fact_id(
        "state",
        subject_entity_id=subject_entity_id,
        field=field,
        target_entity_id=target_entity_id,
    )


def org_fact_id(org_entity_id: str, member_entity_id: str, role: str) -> str:
    """势力、成员、角色；动作与状态写入版本 payload。"""
    return _semantic_fact_id(
        "org", org_entity_id=org_entity_id, member_entity_id=member_entity_id, role=role
    )


def foreshadow_fact_id(clue_anchor: str, related_entity_ids: Sequence[str]) -> str:
    """线索锚点和关联实体。"""
    return _semantic_fact_id(
        "foreshadowing",
        clue_anchor=clue_anchor,
        related_entity_ids=sorted(set(related_entity_ids)),
    )


def event_link_fact_id(
    source_event_id: str, relation_type: EventLinkType, target_event_id: str
) -> str:
    """源事件、关系类型、目标事件。"""
    return _semantic_fact_id(
        "event_link",
        source_event_id=source_event_id,
        relation_type=relation_type.value,
        target_event_id=target_event_id,
    )


def term_definition_fact_id(term_id: str) -> str:
    return _semantic_fact_id("term_definition", term_id=term_id)


def alias_fact_id(canonical_id: str, surface_name: str) -> str:
    """别名事实槽：canonical + 表面名。"""
    return _semantic_fact_id("alias", canonical_id=canonical_id, surface_name=surface_name)


# ---------------------------------------------------------------------------
# evidence / chunk
# ---------------------------------------------------------------------------


def evidence_id(
    claim_version_id_value: str,
    chapter_id: str,
    char_start: int,
    char_end: int,
    span_hash: str,
    verification_version: str = "v1",
    verification_run_id: str = "",
) -> str:
    """基于 claim version 与稳定 source span（§4.3）。

    verification_version / verification_run_id：证据验证成员关系（阶段 11
    十五轮 P1）——同一 claim/span 在不同验证版本或不同验证 run 下产生
    不同 evidence_id，从而**并存**（不覆盖、不删除式升级），历史 run 的
    验证结果可审计。默认 v1/空保持既有调用兼容。
    """
    digest = _digest(
        [
            claim_version_id_value,
            chapter_id,
            str(char_start),
            str(char_end),
            span_hash,
            verification_version,
            verification_run_id,
        ]
    )
    return f"ev_{digest}"


def raw_chunk_id(chapter_id: str, chunking_version: str, anchor_hash: str) -> str:
    """raw_chunk_id = hash(chapter_id + chunking_version + anchor_hash)（§3.3）。"""
    return f"chunk_{_digest([chapter_id, chunking_version, anchor_hash])}"
