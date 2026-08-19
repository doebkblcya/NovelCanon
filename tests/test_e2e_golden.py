"""阶段 05 最小端到端闭环（docs/implementation/05）。

一条命令从空库生成可查询的 active 数据：导入黄金小说 → 建索引 →
run + 固定 Draft（materialize 落库）→ 验证激活 → 结构化/FTS 查询 →
重跑验证幂等。
"""

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from novelcanon.ingestion.service import import_book
from novelcanon.pipeline import (
    ChapterTask,
    NonRetryableError,
    PipelineRunner,
    ProcessResult,
    RetryPolicy,
    RunManager,
    Usage,
    finish_run,
)
from novelcanon.query import QueryService
from novelcanon.retrieval import (
    BruteForceVectorStore,
    FakeEmbedder,
    FakeTokenizer,
    build_index,
    search_shadow,
)
from novelcanon.schemas.types import RunStatus
from novelcanon.storage.repository import Repository
from tests.golden_data import (
    GOLDEN_CHAPTERS,
    MENTION_MAP,
    GoldenClaim,
    GoldenEvidence,
    make_golden_drafts,
)
from tests.helpers import make_fixture_epub

BOOK_ID = "book_golden"


def _build_golden_book(engine: Engine, epub: Path) -> tuple[str, dict[int, str], dict[int, str]]:
    """导入黄金小说，返回 (book_id, ordinal→chapter_id, ordinal→章文本)。"""
    result = import_book(engine, epub, book_id=BOOK_ID)
    repo = Repository(engine)
    chapters = repo.list_chapters(BOOK_ID)
    chapter_ids = {c["ordinal"]: c["chapter_id"] for c in chapters}
    full = repo.get_book_text(BOOK_ID)
    chapter_texts = {c["ordinal"]: full[c["char_start"] : c["char_end"]] for c in chapters}
    assert result.chapter_count == 4
    return result.book_id, chapter_ids, chapter_texts


async def _run_golden_pipeline(
    engine: Engine,
    book_id: str,
    chapter_ids: dict[int, str],
    chapter_texts: dict[int, str],
) -> tuple[str, object]:
    """固定 Draft 跑一遍流水线并激活；返回 (run_id, summary)。"""
    drafts = {d.ordinal: d for d in make_golden_drafts(chapter_ids, chapter_texts)}
    tasks = [
        ChapterTask(
            chapter_id=cid,
            ordinal=ordinal,
            content=chapter_texts[ordinal],
            checkpoint_fields={
                "book_id": book_id,
                "chapter_id": cid,
                "content_hash": hashlib.sha256(chapter_texts[ordinal].encode())
                .hexdigest(),
                "pipeline_version": "golden-p1",
                "prompt_version": "golden-v1",
                "compression_version": "",
                "schema_version": "v1",
            },
        )
        for ordinal, cid in chapter_ids.items()
    ]
    repo = Repository(engine)

    async def process(task: ChapterTask) -> ProcessResult:
        draft = drafts[task.ordinal]
        from novelcanon.extraction import materialize_draft

        materialize_draft(
            engine,
            run_id=_CURRENT_RUN[0],
            book_id=book_id,
            draft=draft,
            canonical_map=MENTION_MAP,
            chapter_text=task.content,
            repo=repo,
        )
        return ProcessResult(
            payload={"chapter_id": task.chapter_id},
            usage=Usage(input_tokens=500, output_tokens=80, provider="golden", model="fixed"),
        )

    _CURRENT_RUN[0] = None
    mgr = RunManager(engine)
    run_id = mgr.create(book_id, input_hash="golden-fixture")
    _CURRENT_RUN[0] = run_id
    assert mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
    runner = PipelineRunner(
        engine,
        run_id,
        book_id,
        concurrency=1,  # materialize 写库，保持单 writer（fixture 量小）
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0.01),
    )
    summary = await runner.run(tasks, process)
    issues = finish_run(engine, run_id, total_chapters=len(tasks), summary=summary)
    assert issues is None, issues
    return run_id, summary


_CURRENT_RUN: list[str | None] = [None]


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            "claims": conn.execute(text("SELECT count(*) FROM claims")).scalar(),
            "evidence": conn.execute(text("SELECT count(*) FROM claim_evidence")).scalar(),
            "entities": conn.execute(text("SELECT count(*) FROM entities")).scalar(),
            "aliases": conn.execute(text("SELECT count(*) FROM entity_alias_claims")).scalar(),
            "mentions": conn.execute(text("SELECT count(*) FROM entity_mentions")).scalar(),
        }


def test_e2e_golden_closed_loop(tmp_path, migrated_db: Engine) -> None:
    """一条命令从空库到可查询 active 数据 + 全部必测查询。"""
    # 1. 黄金小说 → EPUB → 导入
    epub = tmp_path / "golden.epub"
    make_fixture_epub(epub, GOLDEN_CHAPTERS, title="黄金样例")
    book_id, chapter_ids, chapter_texts = _build_golden_book(migrated_db, epub)

    # 2. 建索引（FTS 原文查询用）
    build_index(
        migrated_db,
        book_id,
        tokenizer=FakeTokenizer(),
        embedder=FakeEmbedder(8),
        vector_store=BruteForceVectorStore(8),
    )

    # 3. run + 固定 Draft 落库 + 激活
    run1, _ = asyncio.run(_run_golden_pipeline(migrated_db, book_id, chapter_ids, chapter_texts))

    q = QueryService(migrated_db, book_id)
    mgr = RunManager(migrated_db)

    # ── 必测查询 1：当前状态及证据 ─────────────────────────────
    state = q.current_state("ent_xiaoshi", "cultivation_realm")
    assert state is not None and state["value"] == "元婴"
    assert state["evidence"], "状态必须携带证据"
    ev = state["evidence"][0]
    span = chapter_texts[3][ev["char_start"] : ev["char_end"]]
    assert "元婴" in span  # 证据可回到原文

    # ── 必测查询 2：截止前展示名称 ─────────────────────────────
    assert q.display_name("ent_xiaoshi", knowledge_cutoff=2) == "小石"  # ordinal<3 只含前 3 章
    assert q.display_name("ent_xiaoshi", knowledge_cutoff=3) == "林风"
    # 相似名实体不串：散修林锋的展示名是「林锋」
    assert q.display_name("ent_linfeng") == "林锋"

    # ── 必测查询 3：一跳关系及证据 ─────────────────────────────
    rels = q.one_hop_relations("ent_xiaoshi")
    types = {r["relation_type"] for r in rels}
    assert {"学徒", "未婚夫妻", "师徒", "少主"} <= types
    shitu = next(r for r in rels if r["relation_type"] == "师徒")
    assert shitu["evidence"]

    # ── 必测查询 4：事件参与者 ─────────────────────────────────
    with migrated_db.connect() as conn:
        row = conn.execute(
            text("SELECT claim_version_id FROM event_claims WHERE event_type = '拜师'")
        ).fetchone()
    assert row is not None
    event_info = q.event_participants(row[0])
    assert event_info is not None
    assert {p["entity_id"] for p in event_info["participants"]} == {
        "ent_xiaoshi",
        "ent_qingyunzi",
    }
    assert event_info["event"]["evidence"], "事件必须携带证据与章节定位"
    assert event_info["event"]["observed_chapter_id"] is not None

    # ── 必测查询 5：FTS 原句/关键词 ────────────────────────────
    hits = search_shadow(migrated_db, query="林家少主", book_id=book_id)
    assert hits, "FTS 应召回「林家少主」"
    assert hits[0]["observed_ordinal"] == 3

    # ── 必测查询 6：版本历史（更新前后）────────────────────────
    fact = state["fact_id"]
    history = q.claim_history(fact)
    assert len(history) == 2, "cultivation_realm 应有 2 个版本（金丹→元婴）"
    assert history[0]["observed_ordinal"] == 2  # 金丹
    assert history[1]["observed_ordinal"] == 3  # 元婴
    assert history[1]["supersedes_version_id"] == history[0]["claim_version_id"]
    # 验收 P1：第二版必须是 update，不是 assert
    assert history[1]["operation"] == "update"

    # ── 必测查询 7：knowledge_cutoff 前后差异 ──────────────────
    early = q.current_state("ent_xiaoshi", "cultivation_realm", knowledge_cutoff=2)
    assert early is not None and early["value"] == "金丹", "cutoff=2（前 3 章）时境界应为金丹"
    late = q.current_state("ent_xiaoshi", "cultivation_realm", knowledge_cutoff=3)
    assert late is not None and late["value"] == "元婴"
    # cutoff=3 时后期 alias/身份不可见
    assert q.display_name("ent_xiaoshi", knowledge_cutoff=2) == "小石"  # ordinal<3 只含前 3 章
    rels_early = q.one_hop_relations("ent_xiaoshi", knowledge_cutoff=2)
    assert "少主" not in {r["relation_type"] for r in rels_early}, (
        "少主在第 4 章披露，cutoff=3 不可见"
    )

    # ── 必测查询 8：错误 book_id 返回空（含结构化查询隔离）─────
    assert search_shadow(migrated_db, query="林家少主", book_id="book_nonexistent") == []
    assert q.display_name("ent_xiaoshi", knowledge_cutoff=None) == "林风"  # 对照
    q_other = QueryService(migrated_db, "book_nonexistent")
    assert q_other.display_name("ent_xiaoshi") is None
    assert q_other.entity_state("ent_xiaoshi") == []
    assert q_other.one_hop_relations("ent_xiaoshi") == []
    assert q_other.current_state("ent_xiaoshi", "cultivation_realm") is None
    assert q_other.event_participants(row[0]) is None, "其他书的 QueryService 不得看到本书事件"

    # ── 验证：回答包含章节定位 ─────────────────────────────────
    cite = q.chapter_citation(state["claim_version_id"])
    assert cite is not None and cite["observed_chapter_id"] is not None

    # ── 幂等：整条流水线重跑，事实/证据/实体数量不增加 ─────────
    before = _counts(migrated_db)
    run2, _ = await_pipeline(migrated_db, book_id, chapter_ids, chapter_texts)
    assert mgr.get(run2)["status"] == RunStatus.ACTIVE.value
    assert mgr.get(run1)["status"] == RunStatus.SUPERSEDED.value, "旧 active 必须被 supersede"
    after = _counts(migrated_db)
    assert before == after, f"重跑不得增加数据：{before} → {after}"

    # ── 验收 P0：第二次激活后所有必测查询与第一次一致 ──────────
    assert q.current_state("ent_xiaoshi", "cultivation_realm") == state, (
        "run2 激活后状态查询必须与 run1 一致（成员关系可见性）"
    )
    assert q.display_name("ent_xiaoshi") == "林风"
    assert q.display_name("ent_linfeng") == "林锋"
    assert {"学徒", "未婚夫妻", "师徒", "少主"} <= {
        r["relation_type"] for r in q.one_hop_relations("ent_xiaoshi")
    }
    assert {p["entity_id"] for p in q.event_participants(row[0])["participants"]} == {
        "ent_xiaoshi",
        "ent_qingyunzi",
    }
    # cutoff 语义不变
    assert q.current_state("ent_xiaoshi", "cultivation_realm", knowledge_cutoff=2)["value"] == (
        "金丹"
    )
    assert q.display_name("ent_xiaoshi", knowledge_cutoff=2) == "小石"
    assert "少主" not in {
        r["relation_type"] for r in q.one_hop_relations("ent_xiaoshi", knowledge_cutoff=2)
    }
    assert search_shadow(migrated_db, query="林家少主", book_id=book_id), "run2 激活后 FTS 仍可召回"


def await_pipeline(engine, book_id, chapter_ids, chapter_texts):
    return asyncio.run(_run_golden_pipeline(engine, book_id, chapter_ids, chapter_texts))


def test_e2e_evidence_hash_reproducible(tmp_path, migrated_db: Engine) -> None:
    """所有 direct evidence span hash 100% 复现（05 验证项）。"""
    epub = tmp_path / "golden.epub"
    make_fixture_epub(epub, GOLDEN_CHAPTERS)
    book_id, chapter_ids, chapter_texts = _build_golden_book(migrated_db, epub)
    asyncio.run(_run_golden_pipeline(migrated_db, book_id, chapter_ids, chapter_texts))

    from novelcanon.ingestion.normalize import sha256

    repo = Repository(migrated_db)
    full = repo.get_book_text(book_id)
    with migrated_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT e.evidence_id, e.chapter_id, e.char_start, e.char_end, e.span_hash,"
                " c.ordinal FROM claim_evidence e"
                " JOIN chapters c ON c.chapter_id = e.chapter_id"
            )
        ).fetchall()
    assert rows, "至少应有一条证据"
    for _, chapter_id, cs, ce, span_hash, _ in rows:
        with migrated_db.connect() as conn:
            r = conn.execute(
                text("SELECT char_start, char_end FROM chapters WHERE chapter_id = :c"),
                {"c": chapter_id},
            ).fetchone()
        start = r[0]
        span = full[start + cs : start + ce]
        assert sha256(span) == span_hash, f"证据 {chapter_id}[{cs},{ce}) hash 复现失败"


def test_materialize_idempotent_without_checkpoint(tmp_path, migrated_db: Engine) -> None:
    """验收 P1：绕过 checkpoint 直接重复 materialize 同一 Draft 不增加记录。

    评审复现：entity_mentions 14 → 17（随机 mention_id 破坏幂等）。
    修复：mention_id 必须来自输入（稳定主键），重复落库 INSERT OR IGNORE。
    """
    epub = tmp_path / "golden.epub"
    make_fixture_epub(epub, GOLDEN_CHAPTERS)
    book_id, chapter_ids, chapter_texts = _build_golden_book(migrated_db, epub)
    drafts = {d.ordinal: d for d in make_golden_drafts(chapter_ids, chapter_texts)}

    from novelcanon.extraction import materialize_draft

    repo = Repository(migrated_db)
    run_id = RunManager(migrated_db).create(book_id, input_hash="direct-replay")

    def replay() -> None:
        for ordinal in sorted(drafts):
            materialize_draft(
                migrated_db,
                run_id=run_id,
                book_id=book_id,
                draft=drafts[ordinal],
                canonical_map=MENTION_MAP,
                chapter_text=chapter_texts[ordinal],
                repo=repo,
            )

    replay()
    before = _counts(migrated_db)
    replay()  # 同 run 直接重放同一批 Draft
    after = _counts(migrated_db)
    assert before == after, f"重复 materialize 不得增加数据：{before} → {after}"
    # mention 数量必须等于输入 mention 数（稳定 ID 幂等）
    assert after["mentions"] == 14, "14 个稳定 mention_id 不得因重放翻倍"


def test_failed_run_keeps_active_queries_unchanged(tmp_path, migrated_db: Engine) -> None:
    """验收 P1：失败 run 写入部分 staging 后，旧 active 查询完全不变。"""
    epub = tmp_path / "golden.epub"
    make_fixture_epub(epub, GOLDEN_CHAPTERS)
    book_id, chapter_ids, chapter_texts = _build_golden_book(migrated_db, epub)

    run1, _ = asyncio.run(_run_golden_pipeline(migrated_db, book_id, chapter_ids, chapter_texts))
    q = QueryService(migrated_db, book_id)
    mgr = RunManager(migrated_db)
    state_before = q.current_state("ent_xiaoshi", "cultivation_realm")
    name_before = q.display_name("ent_xiaoshi")
    rels_before = {r["relation_type"] for r in q.one_hop_relations("ent_xiaoshi")}

    # run2：ch1 正常 materialize（含一条 staging 专属 claim），ch2 失败 → 整体 failed
    drafts = {d.ordinal: d for d in make_golden_drafts(chapter_ids, chapter_texts)}
    d0 = drafts[0]
    staging_claim = GoldenClaim(
        claim_type="state",
        payload={"field": "staging_flag", "value": "true", "raw_value": "true"},
        fact_fields={"subject_entity_id": "ent_xiaoshi", "field": "staging_flag"},
        observed_chapter_id=d0.chapter_id,
        observed_ordinal=0,
        evidence=GoldenEvidence(d0.chapter_id, 0, 1, chapter_texts[0][0:1]),
    )
    d0_staged = replace(d0, claims=d0.claims + [staging_claim])
    repo = Repository(migrated_db)

    async def process(task: ChapterTask) -> ProcessResult:
        if task.ordinal == 1:
            raise NonRetryableError("故意失败：该章不可重试")
        from novelcanon.extraction import materialize_draft

        materialize_draft(
            migrated_db,
            run_id=_CURRENT_RUN[0],
            book_id=book_id,
            draft=d0_staged if task.ordinal == 0 else drafts[task.ordinal],
            canonical_map=MENTION_MAP,
            chapter_text=task.content,
            repo=repo,
        )
        return ProcessResult(
            payload={"chapter_id": task.chapter_id},
            usage=Usage(input_tokens=100, output_tokens=20, provider="golden", model="fixed"),
        )

    _CURRENT_RUN[0] = None
    run2 = mgr.create(book_id, input_hash="golden-fail-run")
    _CURRENT_RUN[0] = run2
    assert mgr.transition(run2, RunStatus.CREATED, RunStatus.RUNNING)
    runner = PipelineRunner(
        migrated_db,
        run2,
        book_id,
        concurrency=1,
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0.01),
    )
    tasks = [
        ChapterTask(
            chapter_id=cid,
            ordinal=ordinal,
            content=chapter_texts[ordinal],
            checkpoint_fields={
                "book_id": book_id,
                "chapter_id": cid,
                # content_hash 加前缀使 run2 不复用 run1 的 checkpoint（重新抽取）
                "content_hash": "fail-run-"
                + hashlib.sha256(chapter_texts[ordinal].encode()).hexdigest(),
                "pipeline_version": "golden-p1",
                "prompt_version": "golden-v1",
                "compression_version": "",
                "schema_version": "v1",
            },
        )
        for ordinal, cid in chapter_ids.items()
    ]
    summary = asyncio.run(runner.run(tasks, process))
    issues = finish_run(migrated_db, run2, total_chapters=len(tasks), summary=summary)
    assert issues is not None, "run2 有失败章节，激活必须失败"
    assert mgr.get(run2)["status"] == RunStatus.FAILED.value
    assert mgr.get(run1)["status"] == RunStatus.ACTIVE.value, "旧 active 必须保持可查"

    # staging claim 落库可审计，但不可见
    with migrated_db.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM state_claims WHERE field = 'staging_flag'")
        ).scalar()
    assert n == 1, "失败 run 的 staging 数据应可审计"
    assert q.current_state("ent_xiaoshi", "staging_flag") is None

    # 旧 active 查询完全不变
    assert q.current_state("ent_xiaoshi", "cultivation_realm") == state_before
    assert q.display_name("ent_xiaoshi") == name_before
    assert {r["relation_type"] for r in q.one_hop_relations("ent_xiaoshi")} == rels_before


def test_materialize_rejects_bad_evidence(tmp_path, migrated_db: Engine) -> None:
    """验收 P2：证据必须归属本章且 offset 合法，否则拒绝落库。"""
    epub = tmp_path / "golden.epub"
    make_fixture_epub(epub, GOLDEN_CHAPTERS)
    book_id, chapter_ids, chapter_texts = _build_golden_book(migrated_db, epub)
    drafts = {d.ordinal: d for d in make_golden_drafts(chapter_ids, chapter_texts)}
    d0 = drafts[0]

    from novelcanon.extraction import materialize_draft

    repo = Repository(migrated_db)
    run_id = RunManager(migrated_db).create(book_id, input_hash="bad-evidence")

    def claim_with(evidence: GoldenEvidence) -> GoldenClaim:
        return GoldenClaim(
            claim_type="relation",
            payload={
                "from_entity_id": "ent_xiaoshi",
                "to_entity_id": "ent_tiejian",
                "relation_type": "测试",
                "relation_raw": "测试",
            },
            fact_fields={
                "from_entity_id": "ent_xiaoshi",
                "relation_type": "测试",
                "to_entity_id": "ent_tiejian",
            },
            observed_chapter_id=d0.chapter_id,
            observed_ordinal=0,
            evidence=evidence,
        )

    # offset 越界（章长 < 9999）→ 拒绝
    bad_span = replace(
        d0, claims=d0.claims + [claim_with(GoldenEvidence(d0.chapter_id, 5, 9999, "越界"))]
    )
    with pytest.raises(AssertionError, match="越界"):
        materialize_draft(
            migrated_db,
            run_id=run_id,
            book_id=book_id,
            draft=bad_span,
            canonical_map=MENTION_MAP,
            chapter_text=chapter_texts[0],
            repo=repo,
        )

    # 证据归属其他章 → 拒绝
    other_chapter = chapter_ids[1]
    bad_owner = replace(
        d0,
        claims=d0.claims
        + [claim_with(GoldenEvidence(other_chapter, 0, 1, chapter_texts[0][0:1]))],
    )
    with pytest.raises(AssertionError, match="不一致"):
        materialize_draft(
            migrated_db,
            run_id=run_id,
            book_id=book_id,
            draft=bad_owner,
            canonical_map=MENTION_MAP,
            chapter_text=chapter_texts[0],
            repo=repo,
        )
