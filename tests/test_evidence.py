"""阶段 07 证据对齐与验证黄金测试集（docs/implementation/07）。

覆盖验证项：
- ref 回映射：正确映射 + hash 100% 复现；越界/不匹配 → 错误表，不激活；
- span 候选：字面匹配、排序、锚文本提取；
- 四种 claim status 聚合组合全部有测试；
- primary evidence 始终属于对应 claim version；
- 重跑验证不产生重复 evidence / 错误（幂等）；
- QA 层返回精确章节和原文 span。
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from novelcanon.evidence import (
    EvidenceAggregator,
    EvidenceService,
    RefMapper,
    RefMappingError,
)
from novelcanon.evidence.models import AlignedEvidence
from novelcanon.evidence.span_candidates import extract_anchors
from novelcanon.ingestion.normalize import sha256
from novelcanon.ingestion.service import import_book
from novelcanon.pipeline import RunManager
from novelcanon.query import QueryService
from novelcanon.schemas.draft import (
    ExtractionDraftV1,
    MentionDraft,
    ProvisionalClaim,
    RefSourceSegment,
)
from novelcanon.schemas.payloads import RelationPayload, StatePayload
from novelcanon.schemas.types import (
    ClaimStatus,
    EvidenceStance,
    EvidenceType,
    RunStatus,
)
from novelcanon.storage.repository import Repository
from tests.helpers import make_fixture_epub

BOOK_ID = "book_evidence"

# 一章「真实形态」正文（mention 偏移故意不准，模拟 LLM 通病）
CHAPTER_TEXT = (
    "萧炎站在测验广场上，少年握紧了拳头。他的斗之气只有三段，"
    "周围的目光满是嘲讽。父亲萧战远远看着他，眉头紧锁，一言不发。"
)

# ── 真实形态 Draft：claim 引用 mention_id；mention 偏移 0/len（不准）──


def _mention(mid: str, surface: str, offset_hint: int = 0) -> MentionDraft:
    """构造 mention：偏移故意写 0/长度（LLM 通病，阶段 07 用 surface 重定位）。"""
    return MentionDraft(
        mention_id=mid,
        surface_name=surface,
        char_start=offset_hint,
        char_end=offset_hint + len(surface),
    )


def build_real_draft(chapter_id: str, chapter_text: str | None = None) -> ExtractionDraftV1:
    """模拟真实链路的 Map Draft：mentions 偏移不准，claims 引用 mention_id。

    chapter_text 为实际导入的章节文本（含标题）；缺省用 CHAPTER_TEXT。
    """
    text = chapter_text or CHAPTER_TEXT
    ref = RefSourceSegment(
        segment_id="seg_0",
        char_offset=0,
        segment_content_hash=sha256(text),
    )
    return ExtractionDraftV1(
        book_id=BOOK_ID,
        chapter_id=chapter_id,
        chapter_ordinal=1,
        mentions=[
            _mention("m1", "萧炎", 0),
            _mention("m2", "萧战", 0),
        ],
        provisional_claims=[
            ProvisionalClaim(
                provisional_claim_id="c1",
                claim_type="state",
                payload=StatePayload(
                    field="斗之气等级",
                    value="三段",
                    raw_value="斗之气,三段",
                    subject_entity_id="m1",
                ),
                ref_source_segment_id="seg_0",
            ),
            ProvisionalClaim(
                provisional_claim_id="c2",
                claim_type="relation",
                payload=RelationPayload(
                    from_entity_id="m1",
                    to_entity_id="m2",
                    relation_type="父子",
                    relation_raw="父亲萧战",
                ),
                ref_source_segment_id="seg_0",
            ),
        ],
        ref_source_segments=[ref],
    )


def _book_and_chapter(migrated_db: Engine, tmp_path) -> tuple[str, str, str]:
    """导入一章，返回 (book_id, chapter_id, 实际章节文本)。"""
    epub = tmp_path / "evidence.epub"
    make_fixture_epub(epub, [("第一章 测验", CHAPTER_TEXT)], title="证据测试")
    result = import_book(migrated_db, epub, book_id=BOOK_ID)
    repo = Repository(migrated_db)
    ch = repo.list_chapters(BOOK_ID)[0]
    full = repo.get_book_text(BOOK_ID)
    return result.book_id, ch["chapter_id"], full[ch["char_start"] : ch["char_end"]]


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            "claims": conn.execute(text("SELECT count(*) FROM claims")).scalar(),
            "evidence": conn.execute(text("SELECT count(*) FROM claim_evidence")).scalar(),
            "errors": conn.execute(text("SELECT count(*) FROM evidence_errors")).scalar(),
            "mentions": conn.execute(text("SELECT count(*) FROM entity_mentions")).scalar(),
        }


# ── ref 回映射 ────────────────────────────────────────────────


def test_ref_mapper_hash_reproducible() -> None:
    """direct span hash 复现率 100%：正确 ref 映射到原文区间。"""
    ref = RefSourceSegment(
        segment_id="seg_0",
        char_offset=0,
        segment_content_hash=sha256(CHAPTER_TEXT),
    )
    mapped = RefMapper("ch_1", CHAPTER_TEXT).map([ref])
    assert mapped["seg_0"].char_start == 0
    assert mapped["seg_0"].char_end == len(CHAPTER_TEXT)
    assert sha256(mapped["seg_0"].text) == ref.segment_content_hash


def test_ref_mapper_rejects_bad_hash() -> None:
    """hash 不匹配 → RefMappingError(ref_hash_mismatch)，不得猜测修复。"""
    ref = RefSourceSegment(
        segment_id="seg_0", char_offset=0, segment_content_hash="deadbeef"
    )
    try:
        RefMapper("ch_1", CHAPTER_TEXT).map([ref])
    except RefMappingError as exc:
        assert exc.error_code == "ref_hash_mismatch"
    else:
        raise AssertionError("hash 不匹配必须失败")


def test_ref_mapper_rejects_out_of_range() -> None:
    """char_offset 越界 → RefMappingError(ref_out_of_range)。"""
    ref = RefSourceSegment(
        segment_id="seg_0", char_offset=99999, segment_content_hash="x"
    )
    try:
        RefMapper("ch_1", CHAPTER_TEXT).map([ref])
    except RefMappingError as exc:
        assert exc.error_code == "ref_out_of_range"
    else:
        raise AssertionError("越界必须失败")


def test_ref_mapper_multi_segment_ordering() -> None:
    """多段：相邻 offset 推断终点 + 每段 hash 验证。"""
    seg_text = "第一句。第二句！第三句？"
    refs = [
        RefSourceSegment(segment_id="seg_0", char_offset=0, segment_content_hash=sha256(seg_text[0:4])),
        RefSourceSegment(segment_id="seg_1", char_offset=4, segment_content_hash=sha256(seg_text[4:8])),
        RefSourceSegment(segment_id="seg_2", char_offset=8, segment_content_hash=sha256(seg_text[8:12])),
    ]
    mapped = RefMapper("ch_1", seg_text).map(refs)
    assert mapped["seg_0"].char_end == 4
    assert mapped["seg_1"].char_start == 4
    assert mapped["seg_2"].char_end == len(seg_text)


# ── span 候选与锚文本 ─────────────────────────────────────────


def test_extract_anchors_from_payload() -> None:
    """锚文本提取：mention surface + payload 原文字段。"""
    claim = {
        "payload": {
            "subject_entity_id": "m1",
            "value": "三段",
            "raw_value": "斗之气,三段",
        }
    }
    anchors = extract_anchors(claim, {"m1": "萧炎"})
    texts = {a.text for a in anchors}
    assert "萧炎" in texts
    assert "三段" in texts
    assert "斗之气,三段" in texts


def test_span_candidates_sort_by_match_rate() -> None:
    """候选以句子为单元：同句锚文本合并，match_rate 高优先、span 短优先。"""
    from novelcanon.evidence.span_candidates import AnchorTerm, SpanCandidateGenerator

    gen = SpanCandidateGenerator()
    # 「萧炎」「测验广场」同一句 → 合并 rate=1.0；「萧战」在另一句 → 单句候选
    anchors = [
        AnchorTerm("萧炎", "mention"),
        AnchorTerm("测验广场", "value"),
        AnchorTerm("萧战", "mention"),
    ]
    candidates = gen.generate("ch_1", 0, CHAPTER_TEXT, anchors)
    assert candidates, "锚文本都在段内，必须有候选"
    best = candidates[0]
    # 跨句合并（span 上限内）：萧炎+测验广场+萧战 全部命中 → rate=1.0
    assert best.literal_match_rate == 1.0, (
        f"近距离锚文本必须合并：{best.matched_anchors} rate={best.literal_match_rate}"
    )
    assert "萧炎" in best.span_text and "测验广场" in best.span_text
    assert best.char_start >= 0 and best.char_end <= len(CHAPTER_TEXT)
    assert CHAPTER_TEXT[best.char_start : best.char_end] == best.span_text


# ── 聚合：四种 claim status 组合 ──────────────────────────────


def test_status_aggregation_all_combinations() -> None:
    """四种聚合组合全部有测试（仅 unclear / 仅 supports / 并存 / 仅 refutes）。"""
    agg = EvidenceAggregator()

    def ev(stance: EvidenceStance) -> AlignedEvidence:
        return AlignedEvidence(
            chapter_id="ch",
            char_start=0,
            char_end=1,
            span_text="x",
            stance=stance,
        )

    assert agg.aggregate([]).claim_status == ClaimStatus.UNVERIFIED
    assert agg.aggregate([ev(EvidenceStance.UNCLEAR)]).claim_status == ClaimStatus.UNVERIFIED
    assert agg.aggregate([ev(EvidenceStance.SUPPORTS)]).claim_status == ClaimStatus.SUPPORTED
    assert (
        agg.aggregate(
            [ev(EvidenceStance.SUPPORTS), ev(EvidenceStance.REFUTES)]
        ).claim_status
        == ClaimStatus.CONTESTED
    )
    assert agg.aggregate([ev(EvidenceStance.REFUTES)]).claim_status == ClaimStatus.REJECTED


def test_primary_evidence_is_direct_supports() -> None:
    """primary evidence 选择：第一条 supports+direct。"""
    agg = EvidenceAggregator()
    result = agg.aggregate(
        [
            AlignedEvidence("ch", 0, 1, "a", stance=EvidenceStance.SUPPORTS, evidence_type=EvidenceType.CONTEXTUAL),
            AlignedEvidence("ch", 1, 2, "b", stance=EvidenceStance.SUPPORTS, evidence_type=EvidenceType.DIRECT),
        ]
    )
    assert result.primary is not None
    assert result.primary.evidence_type == EvidenceType.DIRECT
    assert result.primary.span_text == "b"


# ── 端到端：对齐 + materialize ────────────────────────────────


def test_align_materializes_evidence_and_status(tmp_path, migrated_db: Engine) -> None:
    """真实形态 Draft 对齐后：claims/evidence 落库、状态 supported、primary 正确。"""
    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="evidence-align")
    draft = build_real_draft(chapter_id, chapter_text)

    service = EvidenceService(migrated_db)
    stats = service.align_chapter(run_id, book_id, draft, chapter_text, "draft_1")

    assert stats.claims == 2
    assert stats.evidence >= 2
    assert stats.statuses.get("supported", 0) == 2
    assert stats.errors == []

    counts = _counts(migrated_db)
    assert counts["claims"] == 2
    assert counts["evidence"] >= 2
    assert counts["errors"] == 0

    # primary evidence 属于对应 claim version（ver_* 前缀 + 有 evidence 行）
    with migrated_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.claim_version_id, c.primary_evidence_id, c.claim_status,"
                " c.fact_id FROM claims c ORDER BY c.rowid"
            )
        ).mappings().fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["claim_status"] == "supported"
        assert row["primary_evidence_id"]
        with migrated_db.connect() as conn:
            ev = conn.execute(
                text(
                    "SELECT evidence_id FROM claim_evidence WHERE claim_version_id = :v"
                    " AND evidence_id = :e"
                ),
                {"v": row["claim_version_id"], "e": row["primary_evidence_id"]},
            ).fetchone()
        assert ev is not None, "primary evidence 必须属于对应 claim version"

    # evidence span hash 100% 复现（char 区间为章内偏移，切本章原文）
    with migrated_db.connect() as conn:
        evs = conn.execute(
            text("SELECT char_start, char_end, span_hash FROM claim_evidence")
        ).fetchall()
    assert evs
    for cs, ce, span_hash in evs:
        span = chapter_text[cs:ce]
        assert sha256(span) == span_hash, f"证据 [{cs},{ce}) hash 复现失败"

    # QA 层返回精确章节与原文 span
    q = QueryService(migrated_db, book_id)
    cite = q.chapter_citation(rows[0]["claim_version_id"])
    assert cite is not None
    assert cite["observed_chapter_id"] == chapter_id
    assert cite["evidence_chapter"] == chapter_id
    assert cite["char_start"] >= 0 and cite["char_end"] <= len(chapter_text)
    span = chapter_text[cite["char_start"] : cite["char_end"]]
    assert span, "QA 必须能切出原文 span"


def test_align_idempotent_no_duplicate_evidence(tmp_path, migrated_db: Engine) -> None:
    """重跑验证不产生重复 evidence / 错误（幂等）。"""
    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="evidence-idem")
    draft = build_real_draft(chapter_id, chapter_text)
    service = EvidenceService(migrated_db)

    service.align_chapter(run_id, book_id, draft, chapter_text, "draft_1")
    before = _counts(migrated_db)
    service.align_chapter(run_id, book_id, draft, chapter_text, "draft_1")
    after = _counts(migrated_db)
    assert before == after, f"重跑不得增加数据：{before} → {after}"


def test_align_ref_failure_writes_error_not_activate(tmp_path, migrated_db: Engine) -> None:
    """ref hash 不匹配：整章中止，错误入表，无 claim 激活。"""
    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="evidence-badref")
    draft = build_real_draft(chapter_id, chapter_text)
    draft.ref_source_segments = [
        RefSourceSegment(segment_id="seg_0", char_offset=0, segment_content_hash="bad")
    ]

    service = EvidenceService(migrated_db)
    stats = service.align_chapter(run_id, book_id, draft, chapter_text, "draft_1")
    assert stats.claims == 0
    assert len(stats.errors) == 1
    assert stats.errors[0]["error_code"] == "ref_hash_mismatch"

    counts = _counts(migrated_db)
    assert counts["claims"] == 0
    assert counts["errors"] == 1
    assert counts["evidence"] == 0


def test_align_no_anchor_writes_error(tmp_path, migrated_db: Engine) -> None:
    """claim 无锚文本：记录 no_span_found，不伪造证据（该 claim 不落库）。"""
    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="evidence-noanchor")
    draft = build_real_draft(chapter_id, chapter_text)
    # 第一条 claim 引用不存在的 mention（无 surface 可锚定）且无 raw 字段
    draft.provisional_claims[0].payload = StatePayload(
        field="x", value=None, raw_value=None, subject_entity_id="ghost"
    )
    draft.provisional_claims[0].ref_source_segment_id = "seg_0"

    service = EvidenceService(migrated_db)
    stats = service.align_chapter(run_id, book_id, draft, chapter_text, "draft_1")
    assert stats.statuses.get("unverified", 0) >= 1
    assert any(e["error_code"] == "no_span_found" for e in stats.errors)

    # 无证据 claim 不落库（找不到原文的不激活）
    with migrated_db.connect() as conn:
        row = conn.execute(
            text("SELECT count(*) FROM state_claims WHERE field = 'x'")
        ).fetchone()
    assert row[0] == 0, "无锚文本 claim 不得写库"
    # 有证据的 claim 正常落库
    with migrated_db.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM claims")).scalar()
    assert n == 1


def test_align_run_from_staging(tmp_path, migrated_db: Engine) -> None:
    """align_run：从 staging valid Draft 对齐（真实链路入口形态）。"""
    from novelcanon.extraction.staging import MapStaging
    from novelcanon.pipeline import ChapterTask

    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    mgr = RunManager(migrated_db)
    run_id = mgr.create(book_id, input_hash="evidence-staging")
    assert mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)

    draft = build_real_draft(chapter_id, chapter_text)
    with migrated_db.begin() as conn:
        MapStaging().write(
            conn,
            run_id,
            ChapterTask(
                chapter_id=chapter_id,
                ordinal=1,
                content=chapter_text,
                checkpoint_fields={
                    "book_id": book_id,
                    "chapter_id": chapter_id,
                    "content_hash": sha256(chapter_text),
                    "pipeline_version": "map-p1",
                    "prompt_version": "p-v1",
                    "compression_version": "",
                    "schema_version": "v1",
                },
            ),
            {
                "draft": draft.model_dump(mode="json"),
                "status": "valid",
                "validation_issues": [],
                "request_hash": "r",
                "response_hash": "s",
            },
        )

    service = EvidenceService(migrated_db)
    stats = service.align_run(run_id, book_id)
    assert stats.chapters == 1
    assert stats.claims == 2
    assert stats.statuses.get("supported", 0) == 2
    assert _counts(migrated_db)["claims"] == 2
    # run 保持 running（激活在阶段 09 后）
    assert mgr.get(run_id)["status"] == RunStatus.RUNNING.value


def test_align_real_draft_ref_has_pipeline_segments(tmp_path, migrated_db: Engine) -> None:
    """真实链路：pipeline 注入的多段 ref 可回映射（seg 内容 hash 由原文复现）。"""
    from novelcanon.generation.segments import (
        build_ref_segments,
        split_for_window,
    )
    from novelcanon.retrieval.tokenizer import FakeTokenizer

    text = ("第%d句。" * 40) % tuple(range(40))
    segments = split_for_window(text, FakeTokenizer(), 50)
    assert len(segments) > 1
    refs = build_ref_segments("ch_1", segments)
    mapped = RefMapper("ch_1", text).map(refs)
    assert len(mapped) == len(segments)
    for seg in segments:
        span = mapped[seg.segment_id]
        assert span.char_start == seg.char_start
        assert span.char_end == seg.char_end
        assert span.text == seg.content
