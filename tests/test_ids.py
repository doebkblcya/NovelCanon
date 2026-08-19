"""稳定 ID 契约（定版方案 §4.3）：确定性、字段敏感性、run 无关。"""

from novelcanon.schemas.ids import (
    claim_version_id,
    event_fact_id,
    event_link_fact_id,
    evidence_id,
    foreshadow_fact_id,
    new_uuid_id,
    org_fact_id,
    payload_hash,
    raw_chunk_id,
    relation_fact_id,
    state_fact_id,
    term_definition_fact_id,
)
from novelcanon.schemas.types import EventLinkType


def test_fact_ids_are_deterministic() -> None:
    kwargs = dict(from_entity_id="e1", relation_type="师徒", to_entity_id="e2")
    assert relation_fact_id(**kwargs) == relation_fact_id(**kwargs)
    assert state_fact_id("e1", "alive") == state_fact_id("e1", "alive")


def test_relation_fact_id_sensitive_to_fields() -> None:
    base = relation_fact_id("e1", "师徒", "e2")
    assert base != relation_fact_id("e1", "敌人", "e2")  # 类型
    assert base != relation_fact_id("e2", "师徒", "e1")  # 方向
    assert base != relation_fact_id("e1", "师徒", "e2", context="c1")  # 上下文


def test_state_fact_id_excludes_value() -> None:
    """state fact_id 不包含 value：值变化 → 新版本而非新事实（§4.3）。"""
    assert state_fact_id("e1", "cultivation_realm") == state_fact_id("e1", "cultivation_realm")
    assert state_fact_id("e1", "alive") != state_fact_id("e1", "location")


def test_event_fact_id_participant_order_insensitive() -> None:
    a = event_fact_id("战斗", ["e1", "e2"], "loc1", "ch1", 1)
    b = event_fact_id("战斗", ["e2", "e1"], "loc1", "ch1", 1)
    assert a == b
    assert a != event_fact_id("战斗", ["e1", "e2"], "loc1", "ch1", 2)  # 序号敏感


def test_org_fact_id() -> None:
    assert org_fact_id("org1", "e1", "长老") == org_fact_id("org1", "e1", "长老")
    assert org_fact_id("org1", "e1", "长老") != org_fact_id("org1", "e1", "弟子")


def test_event_link_fact_id() -> None:
    a = event_link_fact_id("ev1", EventLinkType.CAUSES, "ev2")
    b = event_link_fact_id("ev1", EventLinkType.CAUSES, "ev2")
    assert a == b
    assert a != event_link_fact_id("ev1", EventLinkType.ENABLES, "ev2")
    assert a != event_link_fact_id("ev2", EventLinkType.CAUSES, "ev1")


def test_foreshadow_and_term_fact_ids() -> None:
    assert foreshadow_fact_id("anchor", ["e1", "e2"]) == foreshadow_fact_id("anchor", ["e2", "e1"])
    assert term_definition_fact_id("term1") == term_definition_fact_id("term1")


def test_claim_version_id_depends_on_payload_and_schema() -> None:
    fact = relation_fact_id("e1", "师徒", "e2")
    p1 = payload_hash({"relation_type": "师徒"})
    p2 = payload_hash({"relation_type": "敌人"})
    assert claim_version_id(fact, p1) == claim_version_id(fact, p1)
    assert claim_version_id(fact, p1) != claim_version_id(fact, p2)
    assert claim_version_id(fact, p1, "v1") != claim_version_id(fact, p1, "v2")


def test_evidence_id_stable_and_sensitive() -> None:
    eid = evidence_id("ver1", "ch1", 10, 20, "hash1")
    assert eid == evidence_id("ver1", "ch1", 10, 20, "hash1")
    assert eid != evidence_id("ver1", "ch1", 10, 21, "hash1")
    assert eid != evidence_id("ver2", "ch1", 10, 20, "hash1")


def test_raw_chunk_id_stable() -> None:
    assert raw_chunk_id("ch1", "v1", "a") == raw_chunk_id("ch1", "v1", "a")
    assert raw_chunk_id("ch1", "v1", "a") != raw_chunk_id("ch1", "v2", "a")


def test_uuid_ids_unique() -> None:
    assert new_uuid_id("ch") != new_uuid_id("ch")
    assert new_uuid_id("ch").startswith("ch_")
