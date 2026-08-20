"""阶段 11 评测框架测试（docs/implementation/11 §P1）。

覆盖验证项：
- 指标计算器（F1 / precision-recall / QA 章节正确率 / 证据复现 / 因果 precision）；
- Pilot 报告结构（三路线指标 + 汇总）；
- 黄金集冻结（golden_set_from_chapters 与 GOLDEN_CHAPTERS 对应）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import Engine

from novelcanon.eval import (
    GoldenQA,
    Metrics,
    evidence_hash_reproduction,
    fact_metrics,
    golden_set_from_chapters,
    qa_chapter_accuracy,
    run_pilot,
)
from novelcanon.eval.golden import GoldenEntityMerge
from novelcanon.ingestion.service import import_book
from novelcanon.pipeline import (
    ChapterTask,
    PipelineRunner,
    ProcessResult,
    RetryPolicy,
    RunManager,
    finish_run,
)
from novelcanon.pipeline.ledger import Usage
from novelcanon.schemas.types import RunStatus
from novelcanon.storage.repository import Repository
from tests.golden_data import GOLDEN_CHAPTERS, MENTION_MAP, make_golden_drafts
from tests.helpers import make_fixture_epub

BOOK_ID = "book_pilot"


# ── 指标单元测试 ──────────────────────────────────────────────


def test_metrics_of_basic() -> None:
    m = Metrics.of({"a", "b"}, {"a", "c"})
    assert m.precision == 0.5 and m.recall == 0.5 and m.f1 == 0.5
    assert Metrics.of(set(), set()).f1 == 0.0
    assert Metrics.of({"a"}, {"a"}).f1 == 1.0


def test_fact_metrics_normalizes_whitespace() -> None:
    m = fact_metrics(["林风 的 境界 = 元婴"], [" 林风   的 境界 = 元婴 "])
    assert m["f1"] == 1.0


def test_qa_chapter_accuracy() -> None:
    qas = [
        GoldenQA("q1", (2, 3), "entity_state"),
        GoldenQA("q2", (0,), "relation"),
    ]
    answers = [
        {"sources": [{"observed_ordinal": 3}, {"observed_ordinal": 2}]},  # 命中
        {"sources": [{"observed_ordinal": 5}]},  # 未命中
    ]
    r = qa_chapter_accuracy(answers, qas)
    assert r["accuracy"] == 0.5
    assert r["per_question"][0]["hit"] is True
    assert r["per_question"][1]["hit"] is False


def test_qa_chapter_accuracy_missing_answer_counts_as_miss() -> None:
    """P0：缺失回答不得抬高准确率——分母恒为全部黄金问题。"""
    qas = [GoldenQA("q1", (2, 3)), GoldenQA("q2", (0,))]
    # 只返回一个答案（少答）：正确的那题命中，缺失的一题记未命中
    answers = [{"sources": [{"observed_ordinal": 3}]}]
    r = qa_chapter_accuracy(answers, qas)
    assert r["accuracy"] == 0.5, f"缺失答案应计入分母：{r}"
    assert r["answered"] == 1
    assert r["per_question"][0]["answered"] is True
    assert r["per_question"][0]["hit"] is True
    assert r["per_question"][1]["answered"] is False
    assert r["per_question"][1]["hit"] is False


def test_evidence_hash_reproduction() -> None:
    from novelcanon.ingestion.normalize import sha256

    spans = ["林风拜入青云宗", "定下三年之约"]
    predicted = {sha256(spans[0])}
    r = evidence_hash_reproduction(spans, predicted)
    assert r["reproduction_rate"] == 0.5
    assert r["reproduced"] == 1


def test_golden_set_frozen() -> None:
    gs = golden_set_from_chapters("book_x")
    assert gs.book_id == "book_x"
    assert len(gs.qas) >= 4
    assert any(m.canonical == "ent_xiaoshi" for m in gs.entity_merges)
    assert GoldenEntityMerge("ent_xiaoshi", ("小石", "林风")) in gs.entity_merges
    assert any(f.claim_type == "state" for f in gs.facts)
    # P0：证据黄金集不得为空（100% 证据复现率可验证）
    assert len(gs.evidence_spans) >= 8, "黄金证据 span 应冻结"
    assert any("金丹" in s for s in gs.evidence_spans)
    # 因果黄金集非空（P0：因果 precision 可验证）
    assert len(gs.causals) >= 1, "黄金因果边应冻结"
    assert "ent_xiaoshi" in gs.core_canonicals


# ── Pilot 端到端（黄金 4 章固定 Draft）─────────────────────────


def _seed_golden_book(migrated_db: Engine, tmp_path: Path) -> tuple[str, dict, dict]:
    """导入 GOLDEN_CHAPTERS + 黄金 drafts 落库 + 激活（复用阶段 05 黄金链路）。"""
    from novelcanon.extraction import materialize_draft

    epub = tmp_path / "pilot.epub"
    make_fixture_epub(epub, GOLDEN_CHAPTERS, title="评测黄金")
    result = import_book(migrated_db, epub, book_id=BOOK_ID)
    repo = Repository(migrated_db)
    chapters = repo.list_chapters(BOOK_ID)
    chapter_ids = {c["ordinal"]: c["chapter_id"] for c in chapters}
    full = repo.get_book_text(BOOK_ID)
    chapter_texts = {c["ordinal"]: full[c["char_start"] : c["char_end"]] for c in chapters}
    assert result.chapter_count == 4

    from novelcanon.retrieval import (
        BruteForceVectorStore,
        FakeEmbedder,
        FakeTokenizer,
        build_index,
    )

    build_index(
        migrated_db,
        BOOK_ID,
        tokenizer=FakeTokenizer(),
        embedder=FakeEmbedder(8),
        vector_store=BruteForceVectorStore(8),
    )

    drafts = {d.ordinal: d for d in make_golden_drafts(chapter_ids, chapter_texts)}
    tasks = [
        ChapterTask(
            chapter_id=cid,
            ordinal=ordinal,
            content=chapter_texts[ordinal],
            checkpoint_fields={
                "book_id": BOOK_ID,
                "chapter_id": cid,
                "content_hash": "pilot-" + str(ordinal),
                "pipeline_version": "pilot",
                "prompt_version": "pilot-v1",
                "compression_version": "",
                "schema_version": "v1",
            },
        )
        for ordinal, cid in chapter_ids.items()
    ]

    state: dict[str, str | None] = {"run": None}

    async def process(task: ChapterTask) -> object:
        materialize_draft(
            migrated_db,
            run_id=state["run"] or "",  # type: ignore[arg-type]
            book_id=BOOK_ID,
            draft=drafts[task.ordinal],
            canonical_map=MENTION_MAP,
            chapter_text=task.content,
            repo=repo,
        )
        return ProcessResult(
            payload={"chapter_id": task.chapter_id},
            usage=Usage(input_tokens=100, output_tokens=30, provider="golden", model="fixed"),
        )

    mgr = RunManager(migrated_db)
    run_id = mgr.create(BOOK_ID, input_hash="pilot-fixture")
    state["run"] = run_id
    assert mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
    runner = PipelineRunner(
        migrated_db,
        run_id,
        BOOK_ID,
        concurrency=1,
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0.01),
    )
    summary = asyncio.run(runner.run(tasks, process))
    # finish_run 已包含 running→validating→ready→active 原子激活
    issues = finish_run(migrated_db, run_id, total_chapters=len(tasks), summary=summary)
    assert issues is None, issues
    _seed_golden_causal_edge(migrated_db, run_id, chapter_ids)
    return BOOK_ID, chapter_ids, chapter_texts


def _seed_golden_causal_edge(engine: Engine, run_id: str, chapter_ids: dict[int, str]) -> None:
    """落库人工标注的 supported 因果边：拜师 → 境界突破（黄金因果边）。"""
    from sqlalchemy import text as sa_text

    from novelcanon.schemas.envelope import ClaimEnvelope
    from novelcanon.schemas.ids import event_link_fact_id
    from novelcanon.schemas.memory import EventLinkRecord
    from novelcanon.schemas.payloads import EventLinkPayload
    from novelcanon.schemas.types import (
        ClaimStatus,
        EventLinkType,
        Operation,
    )
    from novelcanon.storage.repository import Repository, now_iso

    def _event_version(summary: str) -> str:
        with engine.connect() as conn:
            row = conn.execute(
                sa_text(
                    "SELECT c.claim_version_id FROM claims c"
                    " JOIN event_claims e ON e.claim_version_id = c.claim_version_id"
                    " JOIN claim_observations o ON o.claim_version_id = c.claim_version_id"
                    " WHERE e.summary = :s AND o.extraction_run_id = :r"
                    " ORDER BY c.rowid LIMIT 1"
                ),
                {"s": summary, "r": run_id},
            ).fetchone()
        assert row is not None, f"找不到事件 claim：{summary}"
        return row[0]

    src = _event_version("小石拜入青云子门下")
    tgt = _event_version("境界直接突破至金丹期")
    payload = EventLinkPayload(
        source_event_id=src, target_event_id=tgt, relation_type=EventLinkType.CAUSES
    )
    Repository(engine).write_event_link(
        EventLinkRecord(
            envelope=ClaimEnvelope(
                fact_id=event_link_fact_id(src, EventLinkType.CAUSES, tgt),
                claim_version_id="",  # write_event_link 按 payload 确定性生成
                claim_type="event_link",
                operation=Operation.ASSERT,
                claim_status=ClaimStatus.SUPPORTED,
                observed_chapter_id=chapter_ids[2],
                observed_ordinal=2,
                created_by_run_id=run_id,
                created_at=now_iso(),
            ),
            payload=payload,
            verification_method="manual-golden",
            verification_evidence="小石拜入青云子门下 → 入门当日境界直接突破至金丹期",
        )
    )


def test_pilot_report_structure(tmp_path: Path, migrated_db: Engine) -> None:
    """Pilot 报告：三路线指标 + 汇总（黄金 4 章固定 Draft 可运行）。"""
    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)
    report = run_pilot(migrated_db, book_id, golden)
    d = report.to_dict()
    assert d["book_id"] == book_id
    assert "structured" in d["routes"]
    assert "hybrid" in d["routes"]
    routes = d["routes"]["structured"]
    for key in (
        "facts",
        "facts_by_type",
        "entity_merges",
        "evidence_reproduction",
        "causal_edges",
        "qa_chapter_accuracy",
        "latency_ms",
        "usage",
    ):
        assert key in routes, f"structured 路线缺指标 {key}"
    # ── 正式阈值（定版方案 §14.2，P0：假通过清零）────────────────
    # 事实 recall：黄金 7 条事实固定 Draft 全部落库 → recall = 1.0
    assert routes["facts"]["recall"] == 1.0, routes["facts"]
    # 逐类型 recall：state 主体必须命中（subject_entity_id 修复后）
    for ctype in ("state", "relation", "event"):
        assert routes["facts_by_type"][ctype]["recall"] == 1.0, (
            f"{ctype} 事实 recall 应=1.0：{routes['facts_by_type'][ctype]}"
        )
    # 证据 hash 复现：黄金证据 span 全部复现 → rate = 1.0
    ev = routes["evidence_reproduction"]
    assert ev["reproduction_rate"] == 1.0, f"证据复现率应=1.0：{ev}"
    assert ev["golden_count"] >= 8
    # 因果 precision：supported 黄金边命中 → 1.0
    assert routes["causal_edges"]["precision"] == 1.0, routes["causal_edges"]
    # 实体合并 F1（core/all 分层）
    assert routes["entity_merges"]["all"]["f1"] == 1.0, routes["entity_merges"]
    assert routes["entity_merges"]["core"]["f1"] == 1.0, routes["entity_merges"]
    # QA 章节正确率：冻结阈值 0.95（定版方案 §14.2；阶段 11 复审 P0——
    # 结构化 QA 实测 1.0，低于阈值的实现不得通过）
    assert routes["qa_chapter_accuracy"]["accuracy"] >= 0.95, (
        f"QA 章节正确率应 ≥ 0.95：{routes['qa_chapter_accuracy']}"
    )
    # 每个黄金问题都必须有回答（缺失答案不得抬高准确率）
    assert routes["qa_chapter_accuracy"]["answered"] == len(golden.qas)
    # usage 报告（token/成本/吞吐）
    assert "throughput_qps" in routes["usage"]
    assert "cost_estimate_usd" in routes["usage"]
    assert d["summary"]["fact_f1"] == 1.0
    assert d["summary"]["evidence_reproduction_rate"] == 1.0
    assert d["summary"]["entity_merge_f1_core"] == 1.0


def test_pilot_compressed_route_runs_gate(tmp_path: Path, migrated_db: Engine) -> None:
    """压缩路线真实运行：压缩 + 后验 + 决策门（不再返回 not_configured）。"""
    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)
    report = run_pilot(migrated_db, book_id, golden, compressed=True)
    comp = report.routes["compressed"]
    assert comp.get("decision", {}).get("version") == "gate-v2", "压缩路线必须走真实决策门"
    # 确定性压缩保留全部证据 → 证据复现 1.0、事实 recall 1.0
    assert comp["evidence_reproduction"]["reproduction_rate"] == 1.0, comp
    assert comp["facts"]["recall"] == 1.0, comp
    # 黄金章节几乎全部保留（仅尾部空白归一）→ 成本节省不足 → enable=False
    # （诚实决策：压缩未通过决策门，不默认启用）
    assert comp["retention"] >= 0.95, comp
    assert comp["decision"]["enable"] is False
    # fixture 缺省 golden-replay：oracle 辅助验证，硬前置拒绝启用（复审 P0）
    assert comp["extraction_mode"] == "golden-replay"
    assert comp["decision"]["reasons"]["extraction_mode"] is False, comp["decision"]
    # 每万字全链路用量账本（复审 P1）：覆盖 map/rewrite/消歧/证据验证/链接/reduce
    usage = comp["usage"]
    for stage in ("rewrite", "map", "disambiguation", "evidence_verify", "link", "reduce"):
        assert stage in usage["stages"], f"用量账本缺阶段 {stage}"
    assert "per_wan" in usage and "totals" in usage
    # 全链路每万字分母 = 原始语料字数（不得把各阶段 input_chars 相加）
    total_orig = sum(len(t) for t in chapter_texts.values())
    assert usage["totals"]["input_chars"] == total_orig, (
        f"全链路分母必须是原始语料字数：{usage['totals']}"
    )
    assert "compression_enable" in report.summary


def test_map_extractor_converts_provisional_claims(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P0：MapClaimExtractor 把 LLM draft 的 provisional_claims 转成
    GoldenClaimSpec——证据 span 在压缩文本锚定、surface→canonical 消歧、
    usage 累计（不注入黄金 claim 内容，只读 LLM 输出）。"""
    from novelcanon.compression import CompressionService
    from novelcanon.eval.extractor import MapClaimExtractor
    from novelcanon.pipeline import ProcessResult
    from novelcanon.pipeline.ledger import Usage

    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)
    ch0_text = chapter_texts[0]
    comp = CompressionService().compress_book([("0", ch0_text)])[0]

    async def fake_process(task):
        # 模拟「完美 LLM」Map 输出：原文 raw 短语 + 表面名（无黄金 id）
        draft = {
            "book_id": book_id,
            "chapter_id": task.chapter_id,
            "chapter_ordinal": task.ordinal,
            "mentions": [],
            "local_events": [],
            "provisional_claims": [
                {
                    "provisional_claim_id": "c1",
                    "claim_type": "relation",
                    "operation": "assert",
                    "payload": {
                        "from_entity_id": "小石",
                        "to_entity_id": "铁匠",
                        "relation_type": "学徒",
                        "relation_raw": "是镇上铁匠的学徒",
                    },
                },
                {
                    "provisional_claim_id": "c2",
                    "claim_type": "relation",
                    "operation": "assert",
                    "payload": {
                        "from_entity_id": "小石",
                        "to_entity_id": "小荷",
                        "relation_type": "未婚夫妻",
                        "relation_raw": "不存在的证据文本",  # 无法锚定 → 丢弃
                    },
                },
            ],
            "ref_source_segments": [],
            "local_causes": [],
            "cause_candidates": [],
            "unresolved": [],
        }
        return ProcessResult(
            payload={"draft": draft},
            usage=Usage(input_tokens=50, output_tokens=20, provider="fake", model="m"),
        )

    extractor = MapClaimExtractor(fake_process)  # type: ignore[arg-type]
    specs, usage = extractor({0: comp}, golden)
    assert len(specs) == 1, "无法锚定证据的 claim 必须丢弃（诚实 recall）"
    spec = specs[0]
    assert spec.claim_type == "relation"
    assert spec.evidence_span == "是镇上铁匠的学徒"
    assert spec.evidence_span in comp.compressed_text
    # surface → canonical 消歧（黄金 entity_merges 只用于消歧，不注入内容）
    assert spec.payload["from_entity_id"] == "ent_xiaoshi"
    assert spec.payload["to_entity_id"] == "ent_tiejian"


def test_map_extractor_resolves_mention_ids(tmp_path: Path, migrated_db: Engine) -> None:
    """真实冒烟抓到的 bug：LLM 输出的实体引用是 mention_id（如 m1）而非
    表面名——resolve 必须先经本章 draft.mentions 映射到表面名，再做
    surface→canonical 消歧；否则 m1 原样透传 → 重入库 FK 失败。"""
    from novelcanon.compression import CompressionService
    from novelcanon.eval.extractor import MapClaimExtractor
    from novelcanon.pipeline import ProcessResult
    from novelcanon.pipeline.ledger import Usage

    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)
    ch0_text = chapter_texts[0]
    comp = CompressionService().compress_book([("0", ch0_text)])[0]

    async def fake_process(task):
        # 模拟真实 LLM：mentions 数组 + claims 以 mention_id 引用实体
        draft = {
            "book_id": book_id,
            "chapter_id": task.chapter_id,
            "chapter_ordinal": task.ordinal,
            "mentions": [
                {"mention_id": "m1", "surface_name": "小石", "char_start": 0, "char_end": 2},
                {"mention_id": "m2", "surface_name": "铁匠", "char_start": 0, "char_end": 2},
            ],
            "local_events": [],
            "provisional_claims": [
                {
                    "provisional_claim_id": "c1",
                    "claim_type": "relation",
                    "operation": "assert",
                    "payload": {
                        "from_entity_id": "m1",
                        "to_entity_id": "m2",
                        "relation_type": "学徒",
                        "relation_raw": "是镇上铁匠的学徒",
                    },
                }
            ],
            "ref_source_segments": [],
            "local_causes": [],
            "cause_candidates": [],
            "unresolved": [],
        }
        return ProcessResult(
            payload={"draft": draft},
            usage=Usage(input_tokens=50, output_tokens=20, provider="fake", model="m"),
        )

    extractor = MapClaimExtractor(fake_process)  # type: ignore[arg-type]
    specs, usage = extractor({0: comp}, golden)
    assert len(specs) == 1, "mention_id 引用应经 mentions 映射后正常锚定"
    spec = specs[0]
    assert spec.payload["from_entity_id"] == "ent_xiaoshi", (
        f"m1 应先映射为表面名'小石'再消歧为 canonical：{spec.payload}"
    )
    assert spec.payload["to_entity_id"] == "ent_tiejian"
    assert spec.fact_fields["from_entity_id"] == "ent_xiaoshi"
    assert spec.fact_fields["relation_type"] == "学徒"
    assert spec.observed_ordinal == 0
    assert usage.total() == 70, "usage 必须累计各章各段调用"


def test_pilot_llm_map_extractor_wiring(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P0：llm-map 抽取器接入 run_pilot 全链路——extraction_mode 诚实
    标注、决策门 extraction_mode 硬前置通过（reasons=True）、每万字账本
    含 map 阶段 token。漏抽（空 draft）→ recall/证据复现下降 → 不启用。"""
    from novelcanon.eval.extractor import MapClaimExtractor
    from novelcanon.pipeline import ProcessResult
    from novelcanon.pipeline.ledger import Usage

    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)

    async def fake_process(task):
        # 模拟真实 LLM 但抽取结果为空（最坏情况：全部漏抽）
        return ProcessResult(
            payload={
                "draft": {
                    "book_id": book_id,
                    "chapter_id": task.chapter_id,
                    "chapter_ordinal": task.ordinal,
                    "mentions": [],
                    "local_events": [],
                    "provisional_claims": [],
                    "ref_source_segments": [],
                    "local_causes": [],
                    "cause_candidates": [],
                    "unresolved": [],
                }
            },
            usage=Usage(input_tokens=100, output_tokens=30, provider="fake", model="m"),
        )

    report = run_pilot(
        migrated_db,
        book_id,
        golden,
        compressed=True,
        claim_extractor=MapClaimExtractor(fake_process),  # type: ignore[arg-type]
    )
    comp = report.routes["compressed"]
    assert comp["extraction_mode"] == "llm-map"
    assert comp["facts"]["recall"] < 1.0, "漏抽必须反映为事实 recall 下降"
    assert comp["decision"]["enable"] is False
    # 生产抽取模式硬前置通过（llm-map）→ 拒绝原因来自 recall/证据而非模式
    assert comp["decision"]["reasons"]["extraction_mode"] is True, comp["decision"]
    # 每万字全链路账本：map 阶段执行且有 token（fake usage）
    usage = comp["usage"]
    map_stage = usage["stages"]["map"]
    assert map_stage["executed"] is True
    assert map_stage["tokens"] > 0, f"map 阶段应有 LLM token：{map_stage}"
    assert usage["stages"]["rewrite"]["executed"] is True
    assert usage["stages"]["reduce"]["executed"] is False
    assert usage["per_wan"]["tokens"] > 0
    # 全链路每万字分母 = 原始语料字数（map 用压缩后字数，但总账不得重复累计）
    total_orig = sum(len(t) for t in chapter_texts.values())
    assert usage["totals"]["input_chars"] == total_orig, usage["totals"]
    assert map_stage["input_chars"] > 0, "阶段行保留各自 input_chars"


def test_pilot_cutoff_filters(tmp_path: Path, migrated_db: Engine) -> None:
    """Pilot 支持 cutoff：早期 cutoff 的事实 recall 应下降（不泄露后期）。"""
    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)
    full = run_pilot(migrated_db, book_id, golden)
    early = run_pilot(migrated_db, book_id, golden, cutoff=1)
    f_full = full.routes["structured"]["facts"]["recall"]
    f_early = early.routes["structured"]["facts"]["recall"]
    # 黄金事实含 ch2/ch3 内容：cutoff=1 时后期事实不可见 → recall 下降
    assert f_early <= f_full, f"cutoff=1 不应泄露后期事实：early={f_early} full={f_full}"


def test_golden_set_file_roundtrip_and_validation(tmp_path: Path, migrated_db: Engine) -> None:
    """P0：正式黄金集 JSON 文件入口——roundtrip + book 校验（错配拒绝）。"""
    from novelcanon.eval import (
        golden_set_from_file,
        golden_set_to_dict,
        validate_golden_against_book,
    )

    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)
    path = tmp_path / "golden.json"
    import json

    path.write_text(json.dumps(golden_set_to_dict(golden), ensure_ascii=False), encoding="utf-8")

    loaded = golden_set_from_file(path)
    assert loaded.book_id == book_id
    assert len(loaded.qas) == len(golden.qas)
    assert len(loaded.facts) == len(golden.facts)
    assert len(loaded.claims) == len(golden.claims) >= 9
    # 书内容 hash 校验：携带正确 hash → 通过
    from novelcanon.ingestion.normalize import sha256
    from novelcanon.storage.repository import Repository

    full = Repository(migrated_db).get_book_text(book_id)
    # golden_set_from_chapters 不携带 content hash；用 loaded 补上正确值
    import dataclasses

    # P1：正式模式（require_content_hash=True）强制 hash 非空
    errors_no_hash = validate_golden_against_book(
        migrated_db, book_id, loaded, require_content_hash=True
    )
    assert any("book_content_hash" in e for e in errors_no_hash), errors_no_hash
    # fixture 模式（require_content_hash=False）允许无 hash
    assert validate_golden_against_book(migrated_db, book_id, loaded) == []

    loaded = dataclasses.replace(loaded, book_content_hash=sha256(full))
    assert validate_golden_against_book(migrated_db, book_id, loaded) == []
    # 错误 hash → 拒绝
    bad = dataclasses.replace(loaded, book_content_hash="deadbeef")
    errors = validate_golden_against_book(migrated_db, book_id, bad)
    assert any("hash" in e for e in errors)
    # 章节越界 → 拒绝
    from novelcanon.eval import GoldenQA

    out_of_range = dataclasses.replace(loaded, qas=[GoldenQA("越界", (9999,))])
    assert validate_golden_against_book(migrated_db, book_id, out_of_range)
    # 证据 span 不在对应章节 → 拒绝（P1：标注锚点必须真实存在）
    bad_span = dataclasses.replace(
        loaded,
        facts=[dataclasses.replace(f, evidence_span="不存在的证据文本") for f in loaded.facts],
    )
    errors_span = validate_golden_against_book(migrated_db, book_id, bad_span)
    assert any("证据 span" in e for e in errors_span), errors_span
    # schema 版本不符 → 拒绝
    bad_schema = json.loads(path.read_text(encoding="utf-8"))
    bad_schema["schema_version"] = "golden-v0"
    with pytest.raises(ValueError):
        from novelcanon.eval import golden_set_from_dict

        golden_set_from_dict(bad_schema)


def test_compressed_route_lost_evidence_not_skipped(tmp_path: Path, migrated_db: Engine) -> None:
    """P0：压缩删除证据必须进入复现率分母（不得用 anchored 子集冒充 100%）。

    模拟 LLM 漏抽（只返回 ch0 的 claims）：其余 claim 的证据丢失 →
    事实 recall < 1、证据复现率 < 1、决策门 evidence 条件拒绝。
    """
    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)

    def _leaky_extractor(compressed, g):
        # 模拟抽取器漏掉 ch0 之后的所有事实
        return [c for c in g.claims if c.observed_ordinal == 0]

    report = run_pilot(
        migrated_db, book_id, golden, compressed=True, claim_extractor=_leaky_extractor
    )
    comp = report.routes["compressed"]
    # 分母 = 完整黄金证据集（golden.evidence_spans 去重，与基线路线一致）
    total_spans = len(dict.fromkeys(golden.evidence_spans))
    assert comp["evidence_reproduction"]["golden_count"] == total_spans, (
        "复现率分母必须是完整黄金证据集"
    )
    assert comp["facts"]["recall"] < 1.0, "漏抽必须反映为事实 recall 下降"
    assert comp["evidence_reproduction"]["reproduction_rate"] < 1.0, "丢失证据必须计为未复现"
    # 决策门：证据复现退化 → 拒绝启用（即使成本节省满足）
    assert comp["decision"]["enable"] is False
    assert comp["decision"]["reasons"]["evidence"] is False
    # 抽取模式诚实标注
    assert comp["extraction_mode"] == "llm-map"


def test_compressed_route_evidence_denominator_matches_baseline(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """复审 P0：压缩路线证据复现分母与基线一致（golden.evidence_spans）。

    GoldenSet.evidence_spans 与 claims[*].evidence_span 是**独立字段**：
    人工冻结的额外证据只出现在 evidence_spans 时，压缩路线不得改用
    claims 的 span 缩小分母（虚高复现率、可能错误放行决策门）。
    """
    import dataclasses

    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)
    # 冻结一条只存在于 evidence_spans 的额外证据（不在任何 claim 中）
    extra = "额外冻结的证据，不在任何 claim 中"
    rich = dataclasses.replace(golden, evidence_spans=list(golden.evidence_spans) + [extra])
    report = run_pilot(migrated_db, book_id, rich, compressed=True)
    comp = report.routes["compressed"]
    baseline_ev = report.routes["structured"]["evidence_reproduction"]
    # 两路线分母一致：全部冻结证据（含额外 span）
    assert baseline_ev["golden_count"] == len(dict.fromkeys(rich.evidence_spans))
    assert comp["evidence_reproduction"]["golden_count"] == baseline_ev["golden_count"], (
        f"压缩路线分母必须与基线一致：{comp['evidence_reproduction']} vs {baseline_ev}"
    )
    # 额外证据无法复现 → 复现率如实 <1.0（不得用缩小的分母虚高）
    assert comp["evidence_reproduction"]["reproduction_rate"] < 1.0, comp


def test_golden_validation_checks_canonical_evidence_spans(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """复审 P1：正式校验必须覆盖 canonical evidence_spans（复现率分母）。

    - 正式模式（require_content_hash=True）空集合 → 拒绝（复现率无法定义）；
    - 逐项验证 span 存在于原书文本（拼写错误/不存在的 span 直接进分母）；
    - 空字符串项 → 拒绝。
    """
    import dataclasses

    from novelcanon.eval import validate_golden_against_book

    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)

    # 不存在的 canonical span → 拒绝
    bad_span = dataclasses.replace(golden, evidence_spans=["不存在的证据文本"])
    errors = validate_golden_against_book(migrated_db, book_id, bad_span)
    assert any("evidence_spans" in e for e in errors), errors
    # 正式模式空集合 → 拒绝
    empty = dataclasses.replace(golden, evidence_spans=[])
    errors_empty = validate_golden_against_book(
        migrated_db, book_id, empty, require_content_hash=True
    )
    assert any("非空" in e for e in errors_empty), errors_empty
    # 空字符串项 → 拒绝
    blank = dataclasses.replace(golden, evidence_spans=["是镇上铁匠的学徒", ""])
    errors_blank = validate_golden_against_book(migrated_db, book_id, blank)
    assert any("为空字符串" in e for e in errors_blank), errors_blank
    # 正确集合 → 通过（fixture 模式不强制 hash）
    assert validate_golden_against_book(migrated_db, book_id, golden) == []


def test_compressed_route_empty_evidence_spans_no_zerodivision(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """复审 P1：canonical evidence_spans 为空时压缩路线不得除零。

    复现率计算复用 evidence_hash_reproduction（空集 → 0.0），压缩重入
    成功（claims 仍有 span → reingest 执行）也不抛 ZeroDivisionError。
    """
    import dataclasses

    book_id, chapter_ids, chapter_texts = _seed_golden_book(migrated_db, tmp_path)
    golden = golden_set_from_chapters(book_id)
    empty = dataclasses.replace(golden, evidence_spans=[])
    report = run_pilot(migrated_db, book_id, empty, compressed=True)
    comp = report.routes["compressed"]
    ev = comp["evidence_reproduction"]
    assert ev["golden_count"] == 0
    assert ev["reproduction_rate"] == 0.0, f"空集不得除零：{ev}"
    baseline_ev = report.routes["structured"]["evidence_reproduction"]
    assert baseline_ev["golden_count"] == 0 and baseline_ev["reproduction_rate"] == 0.0


def test_build_map_extractor_uses_schema_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实冒烟抓到的 bug：build_map_extractor 曾用 MapPrompts()（schema_json
    为空）→ prompt 无输出 Schema → LLM 自由发挥字段名（如 entity_name），
    与 Draft Schema（surface_name）不符 → 整份 draft 被拒 → 压缩评测
    recall 恒 0。回归：必须携带完整 Draft JSON Schema（含 surface_name）。"""
    from novelcanon.config.settings import AppSettings
    from novelcanon.eval.extractor import build_map_extractor

    captured: dict = {}

    def fake_build_map_process_fn(**kwargs):
        captured["prompts"] = kwargs["prompts"]
        captured["book_id"] = kwargs["book_id"]

        async def fake_fn(task):  # pragma: no cover - 仅构造，不调用
            raise AssertionError("process_fn 不应在构造测试中被调用")

        return fake_fn

    monkeypatch.setattr(
        "novelcanon.extraction.map_pipeline.build_map_process_fn", fake_build_map_process_fn
    )
    settings = AppSettings(
        llm_model="test-model",
        llm_base_url="http://localhost:9/v1",
        llm_api_key="test-key",
        llm_provider="openai-compatible",
        llm_mode="json_object",
        llm_tokenizer="fake-v1",
        llm_context_window=8192,
        llm_max_output=1024,
    )
    extractor = build_map_extractor(settings)
    assert extractor.profile_id == "eval-llm-map"
    prompts = captured["prompts"]
    assert len(prompts.schema_json) > 1000, (
        f"prompt 必须携带完整 Draft JSON Schema：len={len(prompts.schema_json)}"
    )
    assert "surface_name" in prompts.schema_json, (
        "Schema 必须含 surface_name（LLM 输出 entity_name 会被 Draft 校验拒绝）"
    )
