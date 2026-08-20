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
                    raw_value="斗之气只有三段",  # 逐字摘录原文（P1：raw_value 硬锚）
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
    ref = RefSourceSegment(segment_id="seg_0", char_offset=0, segment_content_hash="deadbeef")
    try:
        RefMapper("ch_1", CHAPTER_TEXT).map([ref])
    except RefMappingError as exc:
        assert exc.error_code == "ref_hash_mismatch"
    else:
        raise AssertionError("hash 不匹配必须失败")


def test_ref_mapper_rejects_out_of_range() -> None:
    """char_offset 越界 → RefMappingError(ref_out_of_range)。"""
    ref = RefSourceSegment(segment_id="seg_0", char_offset=99999, segment_content_hash="x")
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
        RefSourceSegment(
            segment_id="seg_0", char_offset=0, segment_content_hash=sha256(seg_text[0:4])
        ),
        RefSourceSegment(
            segment_id="seg_1", char_offset=4, segment_content_hash=sha256(seg_text[4:8])
        ),
        RefSourceSegment(
            segment_id="seg_2", char_offset=8, segment_content_hash=sha256(seg_text[8:12])
        ),
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
        agg.aggregate([ev(EvidenceStance.SUPPORTS), ev(EvidenceStance.REFUTES)]).claim_status
        == ClaimStatus.CONTESTED
    )
    assert agg.aggregate([ev(EvidenceStance.REFUTES)]).claim_status == ClaimStatus.REJECTED


def test_primary_evidence_is_direct_supports() -> None:
    """primary evidence 选择：第一条 supports+direct。"""
    agg = EvidenceAggregator()
    result = agg.aggregate(
        [
            AlignedEvidence(
                "ch",
                0,
                1,
                "a",
                stance=EvidenceStance.SUPPORTS,
                evidence_type=EvidenceType.CONTEXTUAL,
            ),
            AlignedEvidence(
                "ch",
                1,
                2,
                "b",
                stance=EvidenceStance.SUPPORTS,
                evidence_type=EvidenceType.DIRECT,
            ),
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
        rows = (
            conn.execute(
                text(
                    "SELECT c.claim_version_id, c.primary_evidence_id, c.claim_status,"
                    " c.fact_id FROM claims c ORDER BY c.rowid"
                )
            )
            .mappings()
            .fetchall()
        )
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
        row = conn.execute(text("SELECT count(*) FROM state_claims WHERE field = 'x'")).fetchone()
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


# ── P0 回归：字面共现不能判定事实成立 ─────────────────────────


def test_literal_cooccurrence_not_support(tmp_path, migrated_db: Engine) -> None:
    """验收 P0：原文「甲与乙并肩而立」，claim「甲杀死乙」不得 supported。

    锚文本硬锚 = [甲, 乙, 杀死]；原文只有「甲」「乙」命中，「杀死」
    未出现 → hard_match_rate < 1.0 → 不产生 supports 证据 → unverified，
    错误事实不得进入默认查询。
    """
    from novelcanon.evidence.span_candidates import (
        AnchorTerm,
        SpanCandidateGenerator,
    )
    from novelcanon.evidence.verifiers import LiteralVerifier

    text = "甲与乙并肩而立，众人围观。"
    anchors = [
        AnchorTerm("甲", "mention:from_entity_id", hard=True),
        AnchorTerm("乙", "mention:to_entity_id", hard=True),
        AnchorTerm("杀死", "relation_raw", hard=True),
    ]
    candidates = SpanCandidateGenerator().generate("ch_1", 0, text, anchors)
    verifier = LiteralVerifier()
    verified = [v for c in candidates if (v := verifier.verify(c)) is not None]
    assert verified == [], f"「甲与乙并肩而立」不得支持「甲杀死乙」：{verified}"


def test_literal_full_hard_anchor_supports(tmp_path, migrated_db: Engine) -> None:
    """对照：硬锚全命中才支持（原文「甲杀死了乙」→ supports）。"""
    from novelcanon.evidence.span_candidates import (
        AnchorTerm,
        SpanCandidateGenerator,
    )
    from novelcanon.evidence.verifiers import LiteralVerifier

    text = "甲在众人面前杀死了乙，血溅当场。"
    anchors = [
        AnchorTerm("甲", "mention:from_entity_id", hard=True),
        AnchorTerm("乙", "mention:to_entity_id", hard=True),
        AnchorTerm("杀死", "relation_raw", hard=True),
    ]
    candidates = SpanCandidateGenerator().generate("ch_1", 0, text, anchors)
    verifier = LiteralVerifier()
    verified = [v for c in candidates if (v := verifier.verify(c)) is not None]
    assert verified, "硬锚全命中必须产生 supports"
    assert verified[0].stance == EvidenceStance.SUPPORTS
    assert verified[0].evidence_type == EvidenceType.DIRECT


# ── P0 收紧：谓词/动作必须被原文证明（event / org / term_definition）──


def test_event_predicate_summary_must_be_in_text() -> None:
    """验收 P0：事件硬锚必须包含证明谓词/动作的原文表达。

    「甲与乙并肩而立」不得支持「甲杀死乙」事件——participants 共现
    不等于事件成立；summary（谓词表达）逐字出现在原文才支持。
    """
    from novelcanon.evidence.span_candidates import (
        SpanCandidateGenerator,
        extract_anchors,
    )
    from novelcanon.evidence.verifiers import LiteralVerifier

    mentions = {"m1": "甲", "m2": "乙"}
    local_events = [{"event_type": "杀死", "sequence_in_chapter": 1, "participants": ["m1", "m2"]}]
    claim = {
        "claim_type": "event",
        "payload": {
            "event_type": "杀死",
            "summary": "甲杀死乙",
            "location_entity_id": None,
            "sequence_in_chapter": 1,
        },
    }
    anchors = extract_anchors(claim, mentions, local_events=local_events)
    assert any(a.hard and a.source == "summary" for a in anchors), (
        "event summary 必须为硬锚（谓词表达）"
    )
    text = "甲与乙并肩而立，众人围观。"
    candidates = SpanCandidateGenerator().generate("ch_1", 0, text, anchors)
    verified = [v for c in candidates if (v := LiteralVerifier().verify(c)) is not None]
    assert verified == [], f"共现不得支持事件（谓词未被原文证明）：{verified}"

    # 对照：summary 逐字引用原文（模型引用原文写法）→ 支持
    claim2 = {
        "claim_type": "event",
        "payload": {
            "event_type": "杀死",
            "summary": "甲杀死了乙",
            "location_entity_id": None,
            "sequence_in_chapter": 1,
        },
    }
    anchors2 = extract_anchors(claim2, mentions, local_events=local_events)
    text2 = "甲杀死了乙，血溅当场。"
    candidates2 = SpanCandidateGenerator().generate("ch_1", 0, text2, anchors2)
    verified2 = [v for c in candidates2 if (v := LiteralVerifier().verify(c)) is not None]
    assert verified2, "summary 逐字出现在原文必须支持"
    assert verified2[0].stance == EvidenceStance.SUPPORTS


def test_org_action_verb_required() -> None:
    """验收 P0：org 成员共现不能证明「加入/离开」——action 动词组
    任一命中才算谓词被证明，否则 unverified。"""
    from novelcanon.evidence.span_candidates import (
        SpanCandidateGenerator,
        extract_anchors,
    )
    from novelcanon.evidence.verifiers import LiteralVerifier

    mentions = {"m1": "甲", "m2": "乙"}
    claim = {
        "claim_type": "org",
        "payload": {
            "org_entity_id": "m1",
            "member_entity_id": "m2",
            "role": "弟子",
            "action": "join",
        },
    }
    # 共现但无加入动词 → 硬锚（org + member + action 组）未全命中
    anchors = extract_anchors(claim, mentions)
    assert any(a.group == "org_action" for a in anchors), "org action 动词组必须生成"
    text = "甲与乙并肩而立，众人围观。"
    candidates = SpanCandidateGenerator().generate("ch_1", 0, text, anchors)
    verified = [v for c in candidates if (v := LiteralVerifier().verify(c)) is not None]
    assert verified == [], f"成员共现不得支持 org claim：{verified}"
    # 对照：加入动词出现 → 支持
    text2 = "乙加入甲的门派，成为外门弟子。"
    candidates2 = SpanCandidateGenerator().generate("ch_1", 0, text2, anchors)
    verified2 = [v for c in candidates2 if (v := LiteralVerifier().verify(c)) is not None]
    assert verified2, "action 动词命中必须支持 org claim"
    assert verified2[0].stance == EvidenceStance.SUPPORTS


def test_term_definition_requires_definition_in_text() -> None:
    """验收 P0：term_definition 不得靠术语出现即空洞支持——
    definition（谓词表达）为硬锚，须出现在原文。"""
    from novelcanon.evidence.span_candidates import (
        SpanCandidateGenerator,
        extract_anchors,
    )
    from novelcanon.evidence.verifiers import LiteralVerifier

    claim = {
        "claim_type": "term_definition",
        "payload": {"term_id": "t1", "definition": "斗之气是一种修炼境界"},
    }
    anchors = extract_anchors(claim, {})
    assert all(a.hard for a in anchors), "definition 必须为硬锚（此前无硬锚=空洞支持）"
    # 术语出现但定义句未出现 → 无候选 → unverified
    text = "萧炎修炼斗之气已有三年。"
    candidates = SpanCandidateGenerator().generate("ch_1", 0, text, anchors)
    verified = [v for c in candidates if (v := LiteralVerifier().verify(c)) is not None]
    assert verified == [], "术语出现不得支持定义（定义未在原文）"
    # 对照：定义句逐字出现 → 支持
    text2 = "斗之气是一种修炼境界，分为十段。"
    candidates2 = SpanCandidateGenerator().generate("ch_1", 0, text2, anchors)
    verified2 = [v for c in candidates2 if (v := LiteralVerifier().verify(c)) is not None]
    assert verified2, "definition 出现在原文必须支持"
    assert verified2[0].stance == EvidenceStance.SUPPORTS


# ── P0 回归：foreshadowing 含 related_entity_ids 不得崩溃 ──────


def test_align_foreshadowing_with_related_entities(tmp_path, migrated_db: Engine) -> None:
    """验收 P0：含 related_entity_ids 且证据有效的 foreshadowing claim
    必须能完成 align + materialize（此前 _ns_payload 用 dataclasses.field
    函数对象做字典 key → **kwargs 解包抛 TypeError: keywords must be
    strings，导致 align 崩溃）。"""
    from novelcanon.schemas.payloads import ForeshadowPayload

    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="foreshadow-align")
    ref = RefSourceSegment(
        segment_id="seg_0",
        char_offset=0,
        segment_content_hash=sha256(chapter_text),
    )
    draft = ExtractionDraftV1(
        book_id=BOOK_ID,
        chapter_id=chapter_id,
        chapter_ordinal=1,
        mentions=[
            _mention("m1", "萧炎"),
            _mention("m2", "萧战"),
        ],
        provisional_claims=[
            ProvisionalClaim(
                provisional_claim_id="c1",
                claim_type="foreshadowing",
                payload=ForeshadowPayload(
                    clue_anchor="斗之气只有三段",
                    related_entity_ids=["m1", "m2"],
                ),
                ref_source_segment_id="seg_0",
            ),
        ],
        ref_source_segments=[ref],
    )
    service = EvidenceService(migrated_db)
    stats = service.align_chapter(run_id, book_id, draft, chapter_text, "draft_1")
    assert stats.errors == [], f"foreshadowing align 不得报错：{stats.errors}"
    assert stats.evidence >= 1, "clue_anchor + related 实体全命中应有证据"
    assert stats.statuses.get("supported", 0) == 1
    # materialize 落库成功（claims 含 foreshadowing 行）
    with migrated_db.connect() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM claims c"
                " JOIN foreshadow_claims f ON f.claim_version_id = c.claim_version_id"
            )
        ).scalar()
    assert n == 1, "foreshadowing claim 必须 materialize 落库"


def test_align_claim_surface_name_reference(tmp_path, migrated_db: Engine) -> None:
    """真实冒烟抓到的 bug：LLM 输出的 claim payload 实体引用可能是**表面名**
    （如「庇拉尔·特尔内拉」）而非 mention_id——_ns_payload 无条件 ns(mid)
    会生成 ch_xxx_表面名，而 entities 行只按 ns(mention_id) 创建 → FK 失败。
    修复：_AdaptedClaim 反查本章 mentions，表面名 → mention_id → ns。"""
    from novelcanon.schemas.payloads import RelationPayload

    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="evidence-surface")
    draft = build_real_draft(chapter_id, chapter_text)
    # 把 relation claim 的实体引用改成表面名（真实 LLM 行为）
    draft.provisional_claims[1].payload = RelationPayload(
        from_entity_id="萧炎",
        to_entity_id="萧战",
        relation_type="父子",
        relation_raw="父亲萧战",
    )

    service = EvidenceService(migrated_db)
    stats = service.align_chapter(run_id, book_id, draft, chapter_text, "draft_1")
    assert stats.errors == [], f"表面名引用不得报错：{stats.errors}"
    assert stats.statuses.get("supported", 0) >= 2

    with migrated_db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT from_entity_id, to_entity_id FROM relation_claims"
                " ORDER BY rowid DESC LIMIT 1"
            )
        ).fetchone()
    # 表面名必须解析为章级 namespace 主键（ns(mention_id) 形式）
    assert row is not None
    assert row[0].startswith(chapter_id[:12] + "_m"), (
        f"from_entity_id 应为 ns(mention_id)：{row[0]}"
    )
    assert row[1].startswith(chapter_id[:12] + "_m"), f"to_entity_id 应为 ns(mention_id)：{row[1]}"


def test_align_unlisted_surface_gets_synthetic_entity(tmp_path, migrated_db: Engine) -> None:
    """真实冒烟抓到的 bug：LLM 可能引用**未列入本章 mentions** 的表面名
    （如组织/家族名「布恩迪亚家族」）——反查无果时需补确定性 mention
    （ns("sf_" + surface)）并建 entity 行，否则 materialize FK 失败。"""
    from novelcanon.schemas.payloads import RelationPayload

    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="evidence-unlisted")
    draft = build_real_draft(chapter_id, chapter_text)
    # to_entity_id 引用本章 mentions 中不存在的表面名（但原文存在，可锚定）
    draft.provisional_claims[1].payload = RelationPayload(
        from_entity_id="m1",
        to_entity_id="测验广场",
        relation_type="位于",
        relation_raw="测验广场",
    )

    service = EvidenceService(migrated_db)
    stats = service.align_chapter(run_id, book_id, draft, chapter_text, "draft_1")
    assert stats.errors == [], f"未列出表面名不得报错：{stats.errors}"

    with migrated_db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT from_entity_id, to_entity_id FROM relation_claims"
                " ORDER BY rowid DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    assert row[1] == f"{chapter_id[:12]}_sf_测验广场", f"未列出表面名应补 sf_ mention：{row[1]}"
    # entity 行必须存在（FK 完整性）
    with migrated_db.connect() as conn:
        ent = conn.execute(
            text("SELECT canonical_id FROM entities WHERE canonical_id = :cid"),
            {"cid": row[1]},
        ).fetchone()
    assert ent is not None, f"sf_ mention 必须有 entity 行：{row[1]}"


def test_state_raw_value_hard_anchor_value_soft() -> None:
    """P1（十三轮）：state 锚定契约统一——value 是规范化语义值（true/dead
    等，不可能逐字），raw_value 才是原文逐字表述。

    - raw_value 逐字存在 + value 规范化 → 硬锚命中 → 支持；
    - raw_value 是改写句（无法逐字）→ 硬锚缺失 → 拒绝（即使 value 命中）。
    """
    from novelcanon.evidence.span_candidates import SpanCandidateGenerator, extract_anchors
    from novelcanon.evidence.verifiers import LiteralVerifier

    text = "萧炎站在测验广场上，少年握紧了拳头。他的斗之气只有三段，周围的目光满是嘲讽。"
    gen = SpanCandidateGenerator()
    ver = LiteralVerifier()

    def state_claim(raw: str) -> dict:
        return {
            "claim_type": "state",
            "payload": {
                "subject_entity_id": "m1",
                "field": "x",
                "value": "三段",
                "raw_value": raw,
            },
        }

    # ① raw_value 逐字 → 支持
    anchors = extract_anchors(state_claim("斗之气只有三段"), {"m1": "萧炎"})
    hard = {a.text for a in anchors if a.hard}
    assert "斗之气只有三段" in hard, f"raw_value 应为硬锚：{hard}"
    assert "三段" not in hard, f"value 规范化值不得为硬锚：{hard}"
    cands = gen.generate("ch", 0, text, anchors)
    assert any(ver.verify(c) is not None for c in cands), "raw_value 逐字应通过"

    # ② raw_value 改写 → 拒绝（value 虽命中，但不再是硬锚）
    anchors2 = extract_anchors(state_claim("他斗气只有三段水平"), {"m1": "萧炎"})
    cands2 = gen.generate("ch", 0, text, anchors2)
    assert not any(ver.verify(c) is not None for c in cands2), (
        "raw_value 改写句必须拒绝（硬锚缺失）"
    )
