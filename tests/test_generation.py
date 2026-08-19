"""阶段 06：generation 单元测试（prompt 版本化 / 分段 / 7 层校验 / client）。"""

import asyncio
import json

import httpx
import pytest

from novelcanon.generation.client import (
    FakeGenerationClient,
    GenerationClient,
    request_hash,
    response_hash,
)
from novelcanon.generation.parser import DraftValidator, parse_response
from novelcanon.generation.prompts import (
    MapPrompts,
    build_map_prompt,
    default_map_prompts,
)
from novelcanon.generation.segments import (
    build_ref_segments,
    ref_segment_prompt_lines,
    split_for_window,
)
from novelcanon.retrieval.tokenizer import FakeTokenizer
from novelcanon.schemas.draft import ExtractionDraftV1

TOK = FakeTokenizer()


def _profile(**overrides):
    from novelcanon.config.settings import GenerationProfile

    kwargs = dict(
        profile_id="g1",
        context_window=200,
        max_output_tokens=100,
        structured_output_mode="json_object",
        tokenizer_id="fake-v1",
        provider="openai-compatible",
        model="test-model",
        base_url="https://example.invalid/v1",
    )
    kwargs.update(overrides)
    return GenerationProfile(**kwargs)


# ── prompt 版本化（06 §1）──────────────────────────────────────


def test_prompt_version_sensitive_to_all_parts() -> None:
    base = MapPrompts(schema_json="{}")
    v = base.version()
    assert MapPrompts(schema_json="{}").version() == v
    assert MapPrompts(system_instruction="改", schema_json="{}").version() != v
    assert MapPrompts(few_shot=["例"], schema_json="{}").version() != v
    assert MapPrompts(schema_json="{\"x\":1}").version() != v


def test_build_map_prompt_layout() -> None:
    prompts = default_map_prompts()
    text = "第一章 正文"
    prompt = build_map_prompt(prompts, text, chapter_title="第一章", repair_issues=["缺字段"])
    assert "第一章 正文" in prompt
    assert "第一章" in prompt
    assert "[输出 Schema]" in prompt
    assert "缺字段" in prompt  # 结构修复请求附带上次错误
    assert "不得生成 canonical_id" in prompt  # system 明确禁止越界字段（诱导反向约束）


def test_default_prompt_schema_exportable() -> None:
    prompts = default_map_prompts()
    schema = json.loads(prompts.schema_json)
    assert schema["title"] == "ExtractionDraftV1"


# ── 窗口分段（06 §2）────────────────────────────────────────────


def test_split_single_segment_when_fits() -> None:
    text = "云雾山下的小镇里，阿远正在劈柴。镇上人都说，阿远与阿杏定了亲。"
    segs = split_for_window(text, TOK, max_tokens=1000)
    assert len(segs) == 1
    assert segs[0].content == text


def test_split_multi_segment_respects_limit() -> None:
    text = "。".join(f"第{i}句内容" for i in range(20)) + "。"
    segs = split_for_window(text, TOK, max_tokens=40)
    assert len(segs) > 1
    for seg in segs:
        assert seg.token_count <= 40, seg
    # 段间无重叠时能精确拼回原文
    assert "".join(seg.content for seg in segs) == text


def test_split_hard_cut_long_sentence() -> None:
    text = "一" * 100
    segs = split_for_window(text, TOK, max_tokens=30)
    assert len(segs) >= 4
    assert all(seg.token_count <= 30 for seg in segs)


def test_ref_segments_point_to_original() -> None:
    text = "甲。乙。"
    segs = split_for_window(text, TOK, max_tokens=5)
    refs = build_ref_segments("ch1", segs)
    assert len(refs) == len(segs)
    for seg, ref in zip(segs, refs, strict=True):
        assert ref.char_offset == seg.char_start
        assert ref.segment_id == seg.segment_id
        assert len(ref.segment_content_hash) == 64
    lines = ref_segment_prompt_lines("ch1", segs)
    assert lines and "seg_0" in lines[0]


# ── 响应解析（第 1 层）──────────────────────────────────────────


def test_parse_response_strips_fence() -> None:
    raw = '```json\n{"book_id": "b"}\n```'
    parsed, issues = parse_response(raw)
    assert parsed == {"book_id": "b"}
    assert issues == []


def test_parse_response_rejects_invalid_json() -> None:
    parsed, issues = parse_response("{not json")
    assert parsed is None
    assert issues and issues[0].code == "parse_error"


# ── 7 层校验（06 §3）────────────────────────────────────────────


def _validator(chapter_text: str = "正文内容", chapter_id: str = "ch1"):
    return DraftValidator(
        book_id="b1", chapter_id=chapter_id, chapter_ordinal=0, chapter_text=chapter_text
    )


def _minimal_draft(**overrides) -> dict:
    draft = {
        "book_id": "b1",
        "chapter_id": "ch1",
        "chapter_ordinal": 0,
        "mentions": [],
        "local_events": [],
        "provisional_claims": [],
        "ref_source_segments": [
            {"segment_id": "seg_0", "char_offset": 0, "segment_content_hash": "h"}
        ],
        "local_causes": [],
        "cause_candidates": [],
        "unresolved": [],
    }
    draft.update(overrides)
    return draft


def test_validator_accepts_clean_draft() -> None:
    draft, issues = _validator().validate(_minimal_draft())
    assert issues == []
    assert isinstance(draft, ExtractionDraftV1)


def test_validator_rejects_unknown_fields() -> None:
    """第 2/6 层：canonical_id 等越界字段直接拒绝（extra=forbid + disclosure）。"""
    bad = _minimal_draft(canonical_id="ent_x")
    draft, issues = _validator().validate(bad)
    assert draft is None
    assert any(
        i.code in ("disclosure", "schema_error") and "canonical_id" in i.message for i in issues
    )


def test_validator_rejects_duplicate_mention_ids() -> None:
    m = {"mention_id": "m0", "surface_name": "阿远", "char_start": 0, "char_end": 2}
    draft, issues = _validator().validate(_minimal_draft(mentions=[m, m]))
    assert draft is None
    assert any(i.code == "id_ref" and "重复" in i.message for i in issues)


def test_validator_rejects_unresolved_participant() -> None:
    event = {"local_event_id": "e0", "event_type": "拜师", "summary": "x", "participants": ["m_x"]}
    draft, issues = _validator().validate(_minimal_draft(local_events=[event]))
    assert draft is None
    assert any(i.code == "id_ref" and "m_x" in i.message for i in issues)


def test_validator_rejects_unknown_ref_segment() -> None:
    claim = {
        "provisional_claim_id": "c0",
        "claim_type": "state",
        "operation": "assert",
        "ref_source_segment_id": "seg_9",
        "payload": {"field": "x", "value": "y", "raw_value": "y", "subject_entity_id": "e"},
    }
    draft, issues = _validator().validate(_minimal_draft(provisional_claims=[claim]))
    assert draft is None
    assert any(i.code == "id_ref" and "seg_9" in i.message for i in issues)


def test_validator_rejects_out_of_range_ref_offset() -> None:
    draft, issues = _validator(chapter_text="正文").validate(
        _minimal_draft(
            ref_source_segments=[
                {"segment_id": "seg_0", "char_offset": 99, "segment_content_hash": "h"}
            ]
        )
    )
    assert draft is None
    assert any(i.code == "ref_range" for i in issues)


def test_validator_rejects_cross_chapter_event_link() -> None:
    """第 6 层：Map 不得构造跨章事件链接。"""
    claim = {
        "provisional_claim_id": "c0",
        "claim_type": "event_link",
        "operation": "assert",
        "payload": {
            "source_event_id": "e1",
            "target_event_id": "e2",
            "relation_type": "causes",
        },
    }
    draft, issues = _validator().validate(_minimal_draft(provisional_claims=[claim]))
    assert draft is None
    assert any(i.code == "disclosure" for i in issues)


def test_validator_rejects_wrong_book_or_ordinal() -> None:
    draft, issues = _validator().validate(_minimal_draft(book_id="b_other"))
    assert draft is None
    assert any(i.code == "invariant" for i in issues)

    draft, issues = _validator().validate(_minimal_draft(chapter_ordinal=5))
    assert draft is None
    assert any(i.code == "invariant" for i in issues)


# ── client（06 §2/§4）───────────────────────────────────────────


def test_request_hash_stable_and_key_free() -> None:
    h1 = request_hash("prompt-1", model="m", profile_id="p")
    h2 = request_hash("prompt-1", model="m", profile_id="p")
    assert h1 == h2
    assert request_hash("prompt-2", model="m", profile_id="p") != h1
    assert response_hash("resp") == response_hash("resp")


async def _complete_with_transport(profile, handler):
    client = GenerationClient(
        profile,
        api_key="secret-key",
        tokenizer=TOK,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return await client.complete("prompt")


def test_generation_client_success_and_usage() -> None:
    async def main() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["model"] == "test-model"
            assert request.headers["Authorization"] == "Bearer secret-key"
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

        result = await _complete_with_transport(_profile(), handler)
        assert result.raw_text == '{"ok": true}'
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    asyncio.run(main())


def test_generation_client_retries_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def main() -> None:
        result = await _complete_with_transport(_profile(max_retries=5), handler)
        assert result.raw_text == "ok"
        assert calls["n"] == 3
        # P1 修复：provider 内部重试必须入账（失败尝试 2 次 → retry_count=2）
        assert result.usage.retry_count == 2, (
            f"429 重试 2 次必须计入 retry_count：{result.usage.retry_count}"
        )

    asyncio.run(main())


def test_generation_client_retry_exhaustion_discarded() -> None:
    """重试耗尽：最终失败为 retryable 错误（runner 会重试/判失败）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "server busy"})

    async def main() -> None:
        client = GenerationClient(
            _profile(max_retries=3),
            api_key="secret-key",
            tokenizer=TOK,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            await client.complete("prompt")
        except httpx.HTTPStatusError as exc:
            assert exc.response.status_code == 503
        else:
            raise AssertionError("重试耗尽必须抛错（不可静默返回空）")

    asyncio.run(main())
    asyncio.run(main())


def test_generation_client_gives_up_after_retries() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": "down"})

    async def main() -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await _complete_with_transport(_profile(max_retries=2), handler)
        assert calls["n"] == 2  # 最多尝试 2 次

    asyncio.run(main())


def test_fake_generation_client_returns_mapping() -> None:
    fake = FakeGenerationClient({"阿远": '{"ok": 1}'})
    result = asyncio.run(fake.complete("章内有阿远二字"))
    assert result.raw_text == '{"ok": 1}'
    assert fake.calls == ["章内有阿远二字"]
