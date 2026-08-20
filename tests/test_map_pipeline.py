"""阶段 06 e2e：真实 Map 流水线（fake provider）+ 开发样本入 staging。

覆盖 06 验证项：
- 每个成功章节产生 Schema 合法 Draft（fake provider 返回人工标注）；
- Draft 无 canonical_id / 最终 event ID（parser 7 层校验）；
- ref_source_segment 落在原文范围；
- 同配置重复运行命中 checkpoint；
- Token 账本覆盖成功/失败/重试调用；
- 失败章节可单独重跑（invalid 不进入 active，run 保持 running）。
"""

import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import Engine, text

from novelcanon.config.hash import stable_config_hash
from novelcanon.config.settings import GenerationProfile
from novelcanon.extraction.map_pipeline import build_map_process_fn
from novelcanon.extraction.staging import MapStaging
from novelcanon.generation import default_map_prompts, extraction_report
from novelcanon.generation.client import FakeGenerationClient
from novelcanon.ingestion.service import import_book
from novelcanon.pipeline import ChapterTask, PipelineRunner, RunManager
from novelcanon.pipeline.ledger import TokenLedger
from novelcanon.retrieval.tokenizer import FakeTokenizer
from novelcanon.schemas.types import RunStatus
from novelcanon.storage.repository import Repository
from tests.helpers import make_fixture_epub
from tests.samples.map_dev import (
    DEV_BOOK_ID,
    DEV_CHAPTERS,
    build_dev_drafts,
)

TOK = FakeTokenizer()

# 每章正文中的独特片段（fake provider 按 prompt 内容匹配）
_NEEDLES = [
    "阿远正抡着铁锤",
    "药老接过药包",
    "设了拜师酒",
    "一鼓作气通过试炼",
    "失踪的陆家少主",
    "定下三年之约",
    "提升至筑基期",
    "青莲灯",
    "正式加入青云宗外门",
    "等你回来",
]


def _profile(**overrides) -> GenerationProfile:
    kwargs = dict(
        profile_id="dev",
        context_window=8192,
        max_output_tokens=2048,
        structured_output_mode="json_object",
        tokenizer_id="fake-v1",
        provider="fake",
        model="fake-model",
        base_url="https://fake.invalid/v1",
        concurrency_limit=2,
    )
    kwargs.update(overrides)
    return GenerationProfile(**kwargs)


def _build_dev_book(engine: Engine, epub: Path) -> tuple[str, dict[int, str], dict[int, str]]:
    result = import_book(engine, epub, book_id=DEV_BOOK_ID)
    repo = Repository(engine)
    chapters = repo.list_chapters(DEV_BOOK_ID)
    assert result.chapter_count == len(DEV_CHAPTERS) == 10
    chapter_ids = {c["ordinal"]: c["chapter_id"] for c in chapters}
    full = repo.get_book_text(DEV_BOOK_ID)
    chapter_texts = {c["ordinal"]: full[c["char_start"] : c["char_end"]] for c in chapters}
    return result.book_id, chapter_ids, chapter_texts


def _make_tasks(
    book_id: str,
    chapter_ids: dict[int, str],
    chapter_texts: dict[int, str],
    *,
    key_prefix: str = "",
) -> list[ChapterTask]:
    prompts = default_map_prompts()
    schema_version = stable_config_hash(
        {"schema": prompts.schema_json, "profile": _profile().config_hash}
    )
    return [
        ChapterTask(
            chapter_id=cid,
            ordinal=ordinal,
            content=chapter_texts[ordinal],
            checkpoint_fields={
                "book_id": book_id,
                "chapter_id": cid,
                "content_hash": key_prefix
                + hashlib.sha256(chapter_texts[ordinal].encode()).hexdigest(),
                "pipeline_version": "map-p1",
                "prompt_version": prompts.version(),
                "compression_version": "",
                "schema_version": schema_version,
            },
        )
        for ordinal, cid in chapter_ids.items()
    ]


def _fake_client(chapter_ids: dict[int, str], *, respond=None) -> FakeGenerationClient:
    drafts = build_dev_drafts(chapter_ids)
    if respond is not None:
        return FakeGenerationClient(respond)
    mapping = {
        needle: json.dumps(draft.model_dump(mode="json"), ensure_ascii=False)
        for needle, draft in zip(_NEEDLES, drafts, strict=True)
    }
    return FakeGenerationClient(mapping)


async def _run_map(
    engine: Engine,
    book_id: str,
    chapter_ids: dict[int, str],
    chapter_texts: dict[int, str],
    client: FakeGenerationClient,
    *,
    concurrency: int = 2,
    key_prefix: str = "",
) -> tuple[str, object]:
    prompts = default_map_prompts()
    profile = _profile()
    tasks = _make_tasks(book_id, chapter_ids, chapter_texts, key_prefix=key_prefix)
    process_fn = build_map_process_fn(
        book_id=book_id, profile=profile, prompts=prompts, tokenizer=TOK, client=client
    )
    mgr = RunManager(engine)
    run_id = mgr.create(book_id, input_hash="dev-sample")
    assert mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
    runner = PipelineRunner(
        engine,
        run_id,
        book_id,
        concurrency=concurrency,
        staging=MapStaging(),
    )
    summary = await runner.run(tasks, process_fn, stage="map", timeout_seconds=10)
    return run_id, summary


def _staging_counts(engine: Engine, run_id: str) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status, COUNT(*) FROM map_drafts WHERE run_id = :r GROUP BY status"),
            {"r": run_id},
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def _drafts_for(engine: Engine, run_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT draft_json FROM map_drafts WHERE run_id = :r AND status='valid'"),
            {"r": run_id},
        ).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


def test_map_pipeline_dev_samples_to_staging(tmp_path, migrated_db: Engine) -> None:
    """开发样本无人工修复完成 Draft 入 staging：10 章全部 valid + 报告统计。"""
    epub = tmp_path / "dev.epub"
    make_fixture_epub(epub, DEV_CHAPTERS, title="开发样本")
    book_id, chapter_ids, chapter_texts = _build_dev_book(migrated_db, epub)
    client = _fake_client(chapter_ids)

    run_id, summary = asyncio.run(
        _run_map(migrated_db, book_id, chapter_ids, chapter_texts, client)
    )
    assert summary.completed == 10 and summary.failed == 0
    assert summary.reused == 0
    # run 保持 running（Map 只到 staging，阶段 07 验证后 activate）
    assert RunManager(migrated_db).get(run_id)["status"] == RunStatus.RUNNING.value

    counts = _staging_counts(migrated_db, run_id)
    assert counts == {"valid": 10}

    # 每个成功章节都是 Schema 合法 Draft，且不含越界字段
    drafts = _drafts_for(migrated_db, run_id)
    assert len(drafts) == 10
    for draft in drafts:
        assert "canonical_id" not in draft
        assert all("canonical_id" not in (m or {}) for m in draft["mentions"])
        # ref_source_segment 由 pipeline 构造，全部落在本章原文范围
        for ref in draft["ref_source_segments"]:
            assert ref["char_offset"] >= 0

    # 抽取报告按类型统计（relation/state/event/org/foreshadowing 全覆盖）
    report = extraction_report(migrated_db, run_id)
    assert report["chapters"]["valid"] == 10
    assert report["extraction"]["mentions"] > 0
    by_type = report["extraction"]["claims_by_type"]
    assert set(by_type) >= {"relation", "state", "event", "org", "foreshadowing"}
    # 状态更新语义保留（炼气→筑基为 update）
    assert any(c["operation"] == "update" for draft in drafts for c in draft["provisional_claims"])

    # Token 账本覆盖全部成功调用
    tokens = TokenLedger(migrated_db).summary(run_id)
    assert tokens["total"] > 0
    assert tokens["input_tokens"] > 0 and tokens["output_tokens"] > 0


def test_map_checkpoint_reuse_on_repeat_run(tmp_path, migrated_db: Engine) -> None:
    """同配置重复运行命中 checkpoint（06 验证项）；staging 每 run 各 10 行。"""
    epub = tmp_path / "dev.epub"
    make_fixture_epub(epub, DEV_CHAPTERS, title="开发样本")
    book_id, chapter_ids, chapter_texts = _build_dev_book(migrated_db, epub)

    client1 = _fake_client(chapter_ids)
    run1, s1 = asyncio.run(_run_map(migrated_db, book_id, chapter_ids, chapter_texts, client1))
    assert s1.completed == 10 and s1.reused == 0
    assert client1.calls and len(client1.calls) == 10  # 每章一次调用

    # 同配置重跑：全部复用，不调用 provider
    client2 = _fake_client(chapter_ids)
    run2, s2 = asyncio.run(_run_map(migrated_db, book_id, chapter_ids, chapter_texts, client2))
    assert s2.completed == 10 and s2.reused == 10
    assert client2.calls == [], "复用不得再次调用 provider"

    assert _staging_counts(migrated_db, run1) == {"valid": 10}
    assert _staging_counts(migrated_db, run2) == {"valid": 10}
    # draft_id 确定性：同配置重跑得到相同 draft_id
    with migrated_db.connect() as conn:
        ids1 = {
            r[0]
            for r in conn.execute(
                text("SELECT draft_id FROM map_drafts WHERE run_id = :r"), {"r": run1}
            ).fetchall()
        }
        ids2 = {
            r[0]
            for r in conn.execute(
                text("SELECT draft_id FROM map_drafts WHERE run_id = :r"), {"r": run2}
            ).fetchall()
        }
    assert ids1 == ids2


def test_map_failed_chapter_invalid_staging_and_resume(tmp_path, migrated_db: Engine) -> None:
    """失败章节：invalid 入 staging（不进入 active），可单独重跑（06 退出标准）。"""
    epub = tmp_path / "dev.epub"
    make_fixture_epub(epub, DEV_CHAPTERS, title="开发样本")
    book_id, chapter_ids, chapter_texts = _build_dev_book(migrated_db, epub)

    # respond：对「外门」章返回非法 JSON，其余按标注
    drafts = build_dev_drafts(chapter_ids)
    mapping = {
        needle: json.dumps(d.model_dump(mode="json"), ensure_ascii=False)
        for needle, d in zip(_NEEDLES, drafts, strict=True)
    }

    def bad_respond(prompt: str) -> str:
        if "外门" in prompt:
            return "{broken json"
        return next((v for k, v in mapping.items() if k in prompt), "{}")

    client = FakeGenerationClient(bad_respond)
    run1, s1 = asyncio.run(_run_map(migrated_db, book_id, chapter_ids, chapter_texts, client))
    assert s1.completed == 9 and s1.failed == 1

    counts = _staging_counts(migrated_db, run1)
    assert counts.get("valid") == 9
    assert counts.get("invalid") == 1, "非法响应必须保存为 invalid（含错误摘要与响应 hash）"
    with migrated_db.connect() as conn:
        bad = (
            conn.execute(
                text(
                    "SELECT status, error_summary, response_hash, validation_issues FROM map_drafts"
                    " WHERE run_id = :r AND status != 'valid'"
                ),
                {"r": run1},
            )
            .mappings()
            .fetchone()
        )
    assert bad["error_summary"] and "JSON" in bad["error_summary"]
    assert bad["response_hash"]
    assert json.loads(bad["validation_issues"])  # 结构化校验问题

    # 失败章节可单独重跑：修复 provider 后重跑 → 9 章复用 + 1 章重跑
    good_client = _fake_client(chapter_ids)
    run2, s2 = asyncio.run(_run_map(migrated_db, book_id, chapter_ids, chapter_texts, good_client))
    assert s2.reused == 9 and s2.completed == 10 and s2.failed == 0
    assert _staging_counts(migrated_db, run2) == {"valid": 10}


def test_map_repair_retries_structure_errors(tmp_path, migrated_db: Engine) -> None:
    """Schema 错误最多有限次结构修复请求（06 §4）：首错修复后成功。"""
    epub = tmp_path / "dev.epub"
    make_fixture_epub(epub, DEV_CHAPTERS, title="开发样本")
    book_id, chapter_ids, chapter_texts = _build_dev_book(migrated_db, epub)
    drafts = build_dev_drafts(chapter_ids)
    mapping = {
        needle: json.dumps(d.model_dump(mode="json"), ensure_ascii=False)
        for needle, d in zip(_NEEDLES, drafts, strict=True)
    }
    state = {"fixed": False}

    def respond(prompt: str) -> str:
        if "等你回来" in prompt and not state["fixed"]:
            state["fixed"] = True  # 第一次坏，第二次（repair）好
            return "{broken"
        return next((v for k, v in mapping.items() if k in prompt), "{}")

    client = FakeGenerationClient(respond)
    run_id, summary = asyncio.run(
        _run_map(migrated_db, book_id, chapter_ids, chapter_texts, client)
    )
    assert summary.completed == 10 and summary.failed == 0
    assert _staging_counts(migrated_db, run_id) == {"valid": 10}
    assert len(client.calls) == 11  # 10 章 + 1 次结构修复请求
    assert any("上次输出不符合要求" in call for call in client.calls)


def test_map_literal_quote_repair_retries(tmp_path, migrated_db: Engine) -> None:
    """阶段 11 增强 A：逐字字段（summary/relation_raw 等）在 Map 阶段即校验，
    改写句触发一次针对性 repair（repair_issues 带 literal_quote），修复后
    draft 保留；否则证据层才丢弃（no_span_found 62.6% 的根因）。"""
    from tests.samples.map_dev import DEV_CHAPTERS

    epub = tmp_path / "dev.epub"
    make_fixture_epub(epub, DEV_CHAPTERS, title="开发样本")
    book_id, chapter_ids, chapter_texts = _build_dev_book(migrated_db, epub)
    drafts = build_dev_drafts(chapter_ids)
    mapping = {
        needle: json.dumps(d.model_dump(mode="json"), ensure_ascii=False)
        for needle, d in zip(_NEEDLES, drafts, strict=True)
    }
    # 章节 2 的 event summary 首次输出为改写句（非逐字），repair 后恢复逐字
    state = {"repaired": False}
    ch2_needle = "设了拜师酒"
    rewritten = None

    def respond(prompt: str) -> str:
        nonlocal rewritten
        if ch2_needle in prompt and not state["repaired"]:
            state["repaired"] = True
            d = drafts[2].model_dump(mode="json")
            # 改写 summary（"药老收阿远为徒" 非逐字）
            d["provisional_claims"][0]["payload"]["summary"] = "药老收阿远为徒"
            rewritten = json.dumps(d, ensure_ascii=False)
            return rewritten
        if "无法逐字定位" in prompt:
            return json.dumps(drafts[2].model_dump(mode="json"), ensure_ascii=False)
        return next((v for k, v in mapping.items() if k in prompt), "{}")

    client = FakeGenerationClient(respond)
    run_id, summary = asyncio.run(
        _run_map(migrated_db, book_id, chapter_ids, chapter_texts, client)
    )
    assert summary.completed == 10 and summary.failed == 0
    assert _staging_counts(migrated_db, run_id) == {"valid": 10}
    # P2（十三轮）：默认 prompt 也含「逐字」——必须断言 repair issue 专有
    # 文本「无法逐字定位」，且调用次数确实增加（10 章 + 1 次逐字 repair）。
    repair_calls = [c for c in client.calls if "无法逐字定位" in c]
    assert len(repair_calls) == 1, f"应恰好 1 次逐字 repair：{len(repair_calls)}"
    assert len(client.calls) == 11, f"10 章 + 1 repair = 11：{len(client.calls)}"
    # 读回 staging：repair 后 summary 已恢复为原文逐字（非改写句）
    with migrated_db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT draft_json FROM map_drafts d JOIN chapters c ON c.chapter_id=d.chapter_id"
                " WHERE c.book_id = :b AND c.ordinal = 2 AND d.run_id = :r AND d.status = 'valid'"
            ),
            {"b": book_id, "r": run_id},
        ).fetchone()
    assert row is not None, "章节2 应有 valid draft"
    draft = json.loads(row[0])
    summary_final = draft["provisional_claims"][0]["payload"]["summary"]
    assert summary_final == "药老正式收阿远为徒", f"repair 后 summary 应为原文逐字：{summary_final}"
    # usage 必须累计首次 + repair 两次调用（P1：repair 不得重置账本）
    ledger_total = TokenLedger(migrated_db).summary(run_id)["total"]
    assert ledger_total > 0, "token 账本应有值"


def test_map_multi_segment_combines_hashes() -> None:
    """06 修复：多段请求保存全部段的聚合 hash（不只最后一组）。

    验收 P1：request_hash 与 response_hash 各自聚合自己的内容，
    不再复用同一聚合函数（两个字段必须语义可区分、可分别审计）。
    """
    from novelcanon.extraction.map_pipeline import (
        _combine_raw,
        _combine_request_hashes,
        _combine_response_hashes,
    )
    from novelcanon.generation.client import request_hash, response_hash

    class Part:
        def __init__(self, req: str, resp: str, raw: str) -> None:
            self.request_hash = req
            self.response_hash = resp
            self.raw_text = raw

    parts = [
        Part(
            request_hash("seg0", model="m", profile_id="p"),
            response_hash('{"mentions": []}'),
            '{"mentions": []}',
        ),
        Part(
            request_hash("seg1", model="m", profile_id="p"),
            response_hash('{"claims": []}'),
            '{"claims": []}',
        ),
    ]
    req_combined = _combine_request_hashes(parts)
    resp_combined = _combine_response_hashes(parts)
    assert req_combined, "聚合 request hash 非空"
    assert resp_combined, "聚合 response hash 非空"
    # 聚合 hash 与任一单段 hash 不同（证明是全部段的组合）
    assert req_combined != parts[0].request_hash
    assert req_combined != parts[1].request_hash
    # 请求/响应各自聚合 → 两个字段语义不同（不是同一个值）
    assert req_combined != resp_combined, (
        "request/response 聚合必须可区分（此前共用同一聚合函数导致恒等）"
    )
    # 段顺序影响聚合（稳定但敏感）；同段序列聚合稳定
    assert _combine_request_hashes(parts) == req_combined
    assert _combine_response_hashes(parts) == resp_combined
    # 单段请求 hash 变化必须反映到聚合（请求审计有效）
    parts2 = [
        Part(
            request_hash("seg0-changed", model="m", profile_id="p"),
            response_hash('{"mentions": []}'),
            '{"mentions": []}',
        ),
        Part(
            request_hash("seg1", model="m", profile_id="p"),
            response_hash('{"claims": []}'),
            '{"claims": []}',
        ),
    ]
    assert _combine_request_hashes(parts2) != req_combined
    # raw 摘要包含每段的响应 hash 前缀
    raw = _combine_raw(parts)
    assert parts[0].response_hash[:12] in raw
    assert parts[1].response_hash[:12] in raw


def test_literal_quote_check_scoped_to_ref_segment() -> None:
    """P1（十三轮）：逐字校验必须按 claim 的引用段（与证据对齐同范围）——
    引用文字存在于本章其他段时不得误通过。"""
    from novelcanon.generation.parser import LiteralQuoteCheck

    segments = {
        "seg_0": "青石镇的老陈铁匠铺里，阿远正抡着铁锤。",
        "seg_1": "药老正式收阿远为徒，在回春堂设了拜师酒。",
    }
    check = LiteralQuoteCheck("", segments)  # 整章空，仅按段校验
    payload = {
        "provisional_claims": [
            {
                "provisional_claim_id": "c1",
                "claim_type": "event",
                "ref_source_segment_id": "seg_0",
                "payload": {"event_type": "拜师", "summary": "药老正式收阿远为徒"},
            }
        ]
    }
    issues = check.check(payload)
    # summary 只存在于 seg_1，claim 引用 seg_0 → 必须报逐字失败
    assert len(issues) == 1, f"引用段外文字不得通过：{issues}"
    assert issues[0].code == "literal_quote"
    assert "seg_0" in issues[0].message

    # 引用段改为 seg_1 → 通过
    payload["provisional_claims"][0]["ref_source_segment_id"] = "seg_1"
    assert check.check(payload) == [], "引用段内逐字应通过"
