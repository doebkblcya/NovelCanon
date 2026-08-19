"""领域 Schema：模型构造、校验、JSON Schema 导出（ADR-0003）。"""

import pytest
from pydantic import ValidationError

from novelcanon.schemas.draft import ExtractionDraftV1
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.memory import CanonicalMemoryV4
from novelcanon.schemas.payloads import RelationPayload


def _envelope(fact_id: str = "fact_x", run_id: str = "run_1") -> ClaimEnvelope:
    return ClaimEnvelope(
        fact_id=fact_id,
        claim_version_id="ver_x",
        claim_type="relation",
        operation="assert",
        created_by_run_id=run_id,
        created_at="2026-08-19T00:00:00Z",
    )


def test_envelope_defaults() -> None:
    env = _envelope()
    assert env.claim_status.value == "unverified"
    assert env.world_valid_kind.value == "unknown"
    assert env.confidence == 1.0


def test_envelope_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        ClaimEnvelope(
            fact_id="f",
            claim_version_id="v",
            claim_type="relation",
            operation="assert",
            created_by_run_id="r",
            created_at="t",
            confidence=1.5,
        )


def test_draft_constructible() -> None:
    draft = ExtractionDraftV1(book_id="b1", chapter_id="ch1", chapter_ordinal=1)
    assert draft.unresolved == []
    assert draft.provisional_claims == []


def test_memory_constructible() -> None:
    memory = CanonicalMemoryV4(book_id="b1", run_id="run_1")
    assert memory.claims == []


def test_json_schema_exportable() -> None:
    """Pydantic 模型导出 JSON Schema（供 LLM 结构化输出）。"""
    for model in (ClaimEnvelope, ExtractionDraftV1, CanonicalMemoryV4, RelationPayload):
        schema = model.model_json_schema()
        assert schema["type"] == "object"
        assert "properties" in schema


def test_payload_requires_fields() -> None:
    with pytest.raises(ValidationError):
        RelationPayload(from_entity_id="e1", to_entity_id="e2")  # 缺 relation_type
