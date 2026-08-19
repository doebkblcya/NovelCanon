"""阶段 05 最小端到端闭环（docs/implementation/05）。

一条命令从空库生成可查询的 active 数据：导入黄金小说 → 建索引 →
run + 固定 Draft（materialize 落库）→ 验证激活 → 结构化/FTS 查询 →
重跑验证幂等。
"""

import asyncio
from pathlib import Path

from sqlalchemy import Engine, text

from novelcanon.ingestion.service import import_book
from novelcanon.pipeline import (
    ChapterTask,
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
                "content_hash": __import__("hashlib")
                .sha256(chapter_texts[ordinal].encode())
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
    asyncio.run(_run_golden_pipeline(migrated_db, book_id, chapter_ids, chapter_texts))

    q = QueryService(migrated_db)
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
    participants = q.event_participants(row[0])
    assert {p["entity_id"] for p in participants} == {"ent_xiaoshi", "ent_qingyunzi"}

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

    # ── 必测查询 8：错误 book_id 返回空 ────────────────────────
    assert search_shadow(migrated_db, query="林家少主", book_id="book_nonexistent") == []
    assert q.display_name("ent_xiaoshi", knowledge_cutoff=None) == "林风"  # 对照

    # ── 验证：回答包含章节定位 ─────────────────────────────────
    cite = q.chapter_citation(state["claim_version_id"])
    assert cite is not None and cite["observed_chapter_id"] is not None

    # ── 幂等：整条流水线重跑，事实/证据/实体数量不增加 ─────────
    before = _counts(migrated_db)
    run2, _ = await_pipeline(migrated_db, book_id, chapter_ids, chapter_texts)
    assert mgr.get(run2)["status"] == RunStatus.ACTIVE.value
    after = _counts(migrated_db)
    assert before == after, f"重跑不得增加数据：{before} → {after}"


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
