"""阶段 10 答案合成测试（docs/implementation/10 §4）。

覆盖验证项：
- 模型只能接收过滤后上下文（prompt 只含 context，无过滤前全文访问）；
- 区分原文事实与推断（claim/chunk 标 source，模型输出标 caveats）；
- 返回章节定位与 evidence（sources）；
- 证据不足明确拒答（cannot_answer）；
- 记录 query profile、上下文 ID（context hash）与 cutoff 参数；
- 确定性路径（无模型）不调用模型。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from novelcanon.generation.client import FakeGenerationClient
from novelcanon.pipeline.ledger import Usage
from novelcanon.query import ContextItem, SynthesisService

CTX = [
    ContextItem(
        kind="claim",
        claim_type="state",
        claim_version_id="ver_abc",
        chapter_id="ch1",
        observed_ordinal=2,
        char_start=10,
        char_end=30,
        content="萧炎 修为 = 斗之气三段",
        claim_status="supported",
        evidence_stance="supports",
    ),
    ContextItem(
        kind="chunk",
        claim_type="raw_chunk",
        claim_version_id="chunk_x",
        chapter_id="ch2",
        observed_ordinal=3,
        char_start=0,
        char_end=40,
        content="原文片段：三年后，若你还是废物…",
        claim_status="supported",
        evidence_stance="supports",
    ),
]


def test_empty_context_refuses(tmp_path: Path, migrated_db: Engine) -> None:
    svc = SynthesisService(migrated_db, "book_x")
    result = svc.answer(
        "萧炎修为",
        route="structured",
        query_type="entity_state",
        context=[],
        knowledge_cutoff=5,
    )
    assert result.cannot_answer
    assert "证据不足" in result.answer
    assert result.context_id
    assert result.knowledge_cutoff == 5


def test_deterministic_answer_grounds_facts(tmp_path: Path, migrated_db: Engine) -> None:
    svc = SynthesisService(migrated_db, "book_x")
    result = svc.answer(
        "萧炎修为",
        route="structured",
        query_type="entity_state",
        context=CTX,
    )
    assert not result.synthesized  # 确定性路径
    assert "斗之气三段" in result.answer
    assert len(result.sources) == 2
    # 来源带章节定位与 stance
    s0 = result.sources[0]
    assert s0.observed_ordinal == 2
    assert s0.stance == "supports"
    assert result.context_id
    assert result.query_profile == "default"


def test_llm_synthesis_prompt_isolated_from_raw_text(tmp_path: Path, migrated_db: Engine) -> None:
    """prompt 只含过滤后上下文：结构上无法访问过滤前全文（10 §4）。"""
    fake = FakeGenerationClient(
        {
            "上下文": '{"answer":"依据上下文第1条：萧炎修为为斗之气三段",'
            '"confidence":0.9,"caveats":["推断项"]}'
        },
        usage=Usage(input_tokens=50, output_tokens=20, provider="fake", model="m"),
    )
    svc = SynthesisService(migrated_db, "book_x", client=fake, profile_id="p1")
    result = svc.answer(
        "萧炎修为",
        route="structured",
        query_type="entity_state",
        context=CTX,
        knowledge_cutoff=4,
        world_at=2,
    )
    assert result.synthesized
    assert "斗之气三段" in result.answer
    assert result.confidence == 0.9
    assert result.profile_id == "p1"
    assert result.knowledge_cutoff == 4 and result.world_at == 2
    # prompt 记录（只含过滤后上下文与参数）
    prompt = fake.calls[0]
    assert "斗之气三段" in prompt
    assert "原文片段" in prompt
    assert "不得使用模型自身记忆补充" in prompt
    assert result.context_id


def test_llm_unparseable_output_refuses(tmp_path: Path, migrated_db: Engine) -> None:
    fake = FakeGenerationClient({"上下文": "不是JSON"}, usage=Usage())
    svc = SynthesisService(migrated_db, "book_x", client=fake, profile_id="p1")
    result = svc.answer(
        "萧炎修为",
        route="structured",
        query_type="entity_state",
        context=CTX,
    )
    assert result.cannot_answer
    assert "证据不足" in result.answer


def test_context_id_changes_with_context(tmp_path: Path, migrated_db: Engine) -> None:
    svc = SynthesisService(migrated_db, "book_x")
    r1 = svc.answer("q", route="s", query_type="t", context=CTX)
    r2 = svc.answer("q", route="s", query_type="t", context=CTX[:1])
    assert r1.context_id != r2.context_id
    r3 = svc.answer("q", route="s", query_type="t", context=CTX)
    assert r1.context_id == r3.context_id  # 同上下文稳定


def test_context_id_includes_extra_evidence(tmp_path: Path, migrated_db: Engine) -> None:
    import dataclasses

    """P1（四轮）：context_id 哈希包含 extra_evidence（多跳后续边变化即变）。"""
    svc = SynthesisService(migrated_db, "book_x")
    base = CTX[0]
    c1 = ContextItem(
        kind=base.kind,
        claim_type=base.claim_type,
        claim_version_id=base.claim_version_id,
        chapter_id=base.chapter_id,
        observed_ordinal=base.observed_ordinal,
        char_start=base.char_start,
        char_end=base.char_end,
        content=base.content,
    )
    c2 = dataclasses.replace(
        c1,
        extra_evidence=[
            {"claim_version_id": "ver_edge2", "chapter_id": "ch3", "observed_ordinal": 4}
        ],
    )
    r1 = svc.answer("q", route="s", query_type="t", context=[c1])
    r2 = svc.answer("q", route="s", query_type="t", context=[c2])
    assert r1.context_id != r2.context_id, "extra_evidence 变化必须改变 context_id"
    # span 变化也影响
    c3 = dataclasses.replace(
        c1,
        extra_evidence=[
            {"claim_version_id": "ver_edge2", "chapter_id": "ch3", "observed_ordinal": 4},
            {"claim_version_id": "ver_edge3", "chapter_id": "ch5"},
        ],
    )
    r3 = svc.answer("q", route="s", query_type="t", context=[c3])
    assert r2.context_id != r3.context_id
