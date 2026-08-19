"""证据对齐服务（阶段 07，docs/implementation/07）。

编排证据处理链：从 staging 读 valid Map Draft，对每章执行
ref 回映射 -> span 候选 -> 验证 -> materialize（claim_evidence +
claim_status 聚合），失败进入 evidence_errors（不猜测修复后激活）。

幂等：对齐结果按确定性键（claim version + span）写入，重跑不产生
重复 evidence / 错误（07 验证项「重跑验证不会产生重复 evidence」）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import Engine

from novelcanon.config.hash import stable_config_hash
from novelcanon.evidence.aggregator import EvidenceAggregator
from novelcanon.evidence.models import AlignedEvidence, SpanCandidate
from novelcanon.evidence.ref_mapper import RefMapper, RefMappingError
from novelcanon.evidence.span_candidates import (
    SpanCandidateGenerator,
    extract_anchors,
)
from novelcanon.evidence.verifiers import (
    EntailmentVerifier,
    LiteralVerifier,
    NullEntailmentVerifier,
    Verification,
)
from novelcanon.extraction.materialize import GoldenDraftLike, materialize_draft
from novelcanon.schemas.draft import (
    ExtractionDraftV1,
    ProvisionalClaim,
)
from novelcanon.schemas.types import Operation
from novelcanon.storage.repository import Repository

_VERIFY_VERSION = "v1"

# 事件 claim 的 participants 不进入 payload（materialize 从 fact_fields 读）
_EVENT_FACT_FIELDS = (
    "event_type",
    "participants",
    "location_entity_id",
    "chapter_id",
    "sequence_in_chapter",
)


@dataclass
class ChapterAlignStats:
    chapter_id: str
    claims: int = 0
    evidence: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)


@dataclass
class AlignRunStats:
    chapters: int = 0
    claims: int = 0
    evidence: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)


class EvidenceService:
    """阶段 07 证据对齐入口（每章一次对齐 + materialize）。"""

    def __init__(
        self,
        engine: Engine,
        *,
        literal_verifier: LiteralVerifier | None = None,
        entailment_verifier: EntailmentVerifier | None = None,
        aggregator: EvidenceAggregator | None = None,
    ) -> None:
        self._engine = engine
        self._repo = Repository(engine)
        self._literal = literal_verifier or LiteralVerifier()
        self._entailment = entailment_verifier or NullEntailmentVerifier()
        self._aggregator = aggregator or EvidenceAggregator()
        self._candidates = SpanCandidateGenerator()

    # ── 对外：整 run 对齐 ──────────────────────────────────────

    def align_run(self, run_id: str, book_id: str) -> AlignRunStats:
        """对某 run 的全部 valid Draft 做证据对齐并 materialize。

        返回聚合统计；错误写入 evidence_errors（幂等），不抛出。
        """
        drafts = self._repo.list_valid_map_drafts(run_id)
        total = AlignRunStats()
        for row in drafts:
            draft = ExtractionDraftV1.model_validate(json.loads(row["draft_json"]))
            chapter_text = self._repo.chapter_text_for(book_id, draft.chapter_id)
            stats = self.align_chapter(
                run_id, book_id, draft, chapter_text, row["draft_id"]
            )
            total.chapters += 1
            total.claims += stats.claims
            total.evidence += stats.evidence
            for status, n in stats.statuses.items():
                total.statuses[status] = total.statuses.get(status, 0) + n
            total.errors.extend(stats.errors)
        return total

    # ── 单章对齐 ───────────────────────────────────────────────

    def align_chapter(
        self,
        run_id: str,
        book_id: str,
        draft: ExtractionDraftV1,
        chapter_text: str,
        draft_id: str,
    ) -> ChapterAlignStats:
        """单章证据对齐：ref 映射 → 候选 → 验证 → materialize。

        失败（ref 映射失败）记录错误并中止该章（不猜测修复后激活）；
        单 claim 无候选时该 claim 保持 unverified（不伪造证据）。
        """
        stats = ChapterAlignStats(chapter_id=draft.chapter_id)
        try:
            refs = RefMapper(draft.chapter_id, chapter_text).map(
                draft.ref_source_segments
            )
        except RefMappingError as exc:
            self._record_error(
                run_id, book_id, draft.chapter_id, draft_id, "", exc.error_code, exc.message
            )
            stats.errors.append(
                self._error_dict(draft.chapter_id, "", exc.error_code, exc.message)
            )
            return stats

        mention_surface = {m.mention_id: m.surface_name for m in draft.mentions}

        # mention_id 只在章内唯一（Map 契约），materialize 需要全局唯一主键：
        # 章级 namespace 前缀，claims payload 中的 mention 引用同步替换。
        def ns(mid: str) -> str:
            return f"{draft.chapter_id[:12]}_{mid}"

        # 逐 claim 对齐 → (adapted_claim, evidences)
        local_events = [e.model_dump(mode="json") for e in draft.local_events]
        aligned: list[tuple[_AdaptedClaim, list[AlignedEvidence]]] = []
        for claim in draft.provisional_claims:
            evidences = self._align_claim(
                draft, claim, refs, mention_surface, run_id, book_id, draft_id, stats
            )
            aligned.append(
                (_AdaptedClaim(claim, draft, evidences, ns, local_events), evidences)
            )

        # 只有找到证据的 claim 才 materialize（找不到原文的不落库，
        # 保持 unverified 语义 + 避免无实体引用写库失败，07 退出标准）
        with_evidence = [(c, evs) for c, evs in aligned if evs]
        if with_evidence:
            materialize_draft(
                self._engine,
                run_id=run_id,
                book_id=book_id,
                draft=cast(GoldenDraftLike, _AlignedDraft(  # 结构匹配（Protocol）
                    draft,
                    [c for c, _ in with_evidence],
                    ns,
                )),
                canonical_map={ns(m_id): ns(m_id) for m_id in mention_surface},
                chapter_text=chapter_text,
                repo=self._repo,
            )

        for _, evidences in aligned:
            stats.claims += 1
            result = self._aggregator.aggregate(evidences)
            stats.statuses[result.claim_status.value] = (
                stats.statuses.get(result.claim_status.value, 0) + 1
            )
            stats.evidence += len(evidences)
        return stats

    # ── 单 claim 对齐 ──────────────────────────────────────────

    def _align_claim(
        self,
        draft: ExtractionDraftV1,
        claim: ProvisionalClaim,
        refs: dict,
        mention_surface: dict[str, str],
        run_id: str,
        book_id: str,
        draft_id: str,
        stats: ChapterAlignStats,
    ) -> list[AlignedEvidence]:
        """为一个 provisional_claim 生成证据。

        - ref_source_segment_id → 段范围；
        - 锚文本 → span 候选 → 字面验证；
        - 低置信（rate < 0.5）→ 可选 entailment verifier；
        - 无证据时记录 no_span_found（该 claim 保持 unverified）。
        """
        claim_dict = claim.model_dump(mode="json")
        seg_id = claim_dict.get("ref_source_segment_id")
        if seg_id not in refs:
            message = f"claim {claim.provisional_claim_id} 引用不存在的段 {seg_id}"
            self._record_error(
                run_id, book_id, draft.chapter_id, draft_id,
                claim.provisional_claim_id, "ref_missing", message,
            )
            stats.errors.append(
                self._error_dict(
                    draft.chapter_id, claim.provisional_claim_id, "ref_missing", message
                )
            )
            return []

        seg = refs[seg_id]
        anchors = extract_anchors(
            claim_dict,
            mention_surface,
            local_events=[e.model_dump(mode="json") for e in draft.local_events],
        )
        if not anchors:
            message = f"claim {claim.provisional_claim_id} 无可用锚文本"
            self._record_error(
                run_id, book_id, draft.chapter_id, draft_id,
                claim.provisional_claim_id, "no_span_found", message,
            )
            stats.errors.append(
                self._error_dict(
                    draft.chapter_id, claim.provisional_claim_id, "no_span_found", message
                )
            )
            return []

        candidates = self._candidates.generate(
            draft.chapter_id, seg.char_start, seg.text, anchors
        )
        for candidate in candidates:
            verification = self._literal.verify(candidate)
            if verification is not None:
                return [self._to_aligned(candidate, verification)]
        for candidate in candidates[:1]:
            verification = self._entailment.verify(candidate)
            if verification is not None:
                return [self._to_aligned(candidate, verification)]
        message = f"claim {claim.provisional_claim_id} 在段 {seg_id} 内无匹配候选"
        self._record_error(
            run_id, book_id, draft.chapter_id, draft_id,
            claim.provisional_claim_id, "no_span_found", message,
        )
        stats.errors.append(
            self._error_dict(draft.chapter_id, claim.provisional_claim_id, "no_span_found", message)
        )
        return []

    @staticmethod
    def _to_aligned(
        candidate: SpanCandidate, verification: Verification
    ) -> AlignedEvidence:
        return AlignedEvidence(
            chapter_id=candidate.chapter_id,
            char_start=candidate.char_start,
            char_end=candidate.char_end,
            span_text=candidate.span_text,
            stance=verification.stance,
            evidence_type=verification.evidence_type,
            literal_match_rate=verification.literal_match_rate,
            verification_method=f"{verification.method}/{_VERIFY_VERSION}",
        )

    # ── 错误记录（幂等：确定性 error_id）──────────────────────

    def _record_error(
        self,
        run_id: str,
        book_id: str,
        chapter_id: str,
        draft_id: str,
        claim_id: str,
        error_code: str,
        message: str,
    ) -> None:
        stage = "ref_mapping" if error_code.startswith("ref_") else "span_candidate"
        error_id = "ev_err_" + stable_config_hash(
            {
                "run": run_id,
                "chapter": chapter_id,
                "claim": claim_id,
                "stage": stage,
                "code": error_code,
            }
        )[:16]
        self._repo.write_evidence_error(
            error_id=error_id,
            run_id=run_id,
            book_id=book_id,
            chapter_id=chapter_id,
            claim_id=claim_id,
            stage=stage,
            error_code=error_code,
            message=message,
        )

    @staticmethod
    def _error_dict(
        chapter_id: str, claim_id: str, error_code: str, message: str
    ) -> dict:
        return {
            "chapter_id": chapter_id,
            "claim_id": claim_id,
            "error_code": error_code,
            "message": message,
        }


# ── materialize 适配层：ExtractionDraftV1 -> GoldenDraftLike ──


class _AlignedDraft:
    """把 ExtractionDraftV1 适配为 materialize 的 GoldenDraftLike。"""

    def __init__(
        self,
        draft: ExtractionDraftV1,
        claims: list[_AdaptedClaim],
        ns=None,
    ) -> None:
        self._draft = draft
        self._claims = claims
        self._ns = ns or (lambda mid: mid)

    @property
    def chapter_id(self) -> str:
        return self._draft.chapter_id

    @property
    def ordinal(self) -> int:
        return self._draft.chapter_ordinal

    @property
    def mentions(self) -> list[tuple[str, str]]:
        return [
            (self._ns(m.mention_id), m.surface_name) for m in self._draft.mentions
        ]

    @property
    def claims(self) -> list[_AdaptedClaim]:
        return self._claims

    @property
    def entity_tiers(self) -> dict:
        return {}


class _AdaptedClaim:
    """把 provisional_claim 适配为 GoldenClaimLike（evidence 由对齐注入）。

    ns：mention_id 章级 namespace（全局唯一主键，阶段 07）。
    local_events：本章 local_events（阶段 09 全局事件标识用——event
    claim 的 participants 不在 payload，而在 local_events 中）。
    """

    def __init__(
        self,
        claim: ProvisionalClaim,
        draft: ExtractionDraftV1,
        evidences: list[AlignedEvidence],
        ns=None,
        local_events: list[dict] | None = None,
    ) -> None:
        self._claim = claim
        self._draft = draft
        self._evidences = evidences
        self._ns = ns or (lambda mid: mid)
        self._local_events = local_events or []

    def _ns_payload(self, payload: dict) -> dict:
        """把 payload 中的 mention 引用字段替换为章级 namespace。"""
        out = dict(payload)
        for fld in (
            "subject_entity_id",
            "from_entity_id",
            "to_entity_id",
            "org_entity_id",
            "member_entity_id",
            "location_entity_id",
            "target_entity_id",
        ):
            mid = out.get(fld)
            if isinstance(mid, str):
                out[fld] = self._ns(mid)
        for fld in ("related_entity_ids", "participants"):
            ids = out.get(fld)
            if isinstance(ids, list):
                # P0 修复：循环变量是 fld，不能用 dataclasses.field（函数对象）
                # 做字典 key——否则 dict 含非字符串 key，**kwargs 解包时抛
                # TypeError: keywords must be strings（foreshadowing 崩溃）。
                out[fld] = [self._ns(m) if isinstance(m, str) else m for m in ids]
        return out

    @property
    def claim_type(self) -> str:
        return self._claim.claim_type.value

    @property
    def operation(self) -> Operation:
        return self._claim.operation

    @property
    def fact_fields(self) -> dict:
        payload = self._ns_payload(self._claim.payload.model_dump(mode="json"))
        ctype = self._claim.claim_type.value
        if ctype == "state":
            return {
                "subject_entity_id": payload["subject_entity_id"],
                "field": payload["field"],
            }
        if ctype == "relation":
            return {
                "from_entity_id": payload["from_entity_id"],
                "relation_type": payload["relation_type"],
                "to_entity_id": payload["to_entity_id"],
            }
        if ctype == "event":
            # participants 在 local_events（Map 契约：EventPayload 不复制
            # participants）；按 event_type + sequence 对齐（09 §1 全局事件标识）
            participants = self._match_event_participants(payload)
            return {
                "event_type": payload["event_type"],
                "participants": participants,
                "location_entity_id": payload.get("location_entity_id"),
                "chapter_id": self._draft.chapter_id,
                "sequence_in_chapter": payload.get("sequence_in_chapter", 0),
            }
        if ctype == "org":
            return {
                "org_entity_id": payload["org_entity_id"],
                "member_entity_id": payload["member_entity_id"],
                "role": payload.get("role", ""),
            }
        if ctype == "foreshadowing":
            return {
                "clue_anchor": payload["clue_anchor"],
                "related_entity_ids": payload.get("related_entity_ids", []),
            }
        if ctype == "term_definition":
            return {"term_id": payload["term_id"]}
        return dict(payload)

    def _match_event_participants(self, payload: dict) -> list[str]:
        """按 event_type + 章内顺序从 local_events 匹配 participants。

        local_events 与 event claims 不一定同序（章3：穿越/测试/谈话 vs
        测试/穿越/修炼/谈话），用「同 type 第 N 个」对齐：对每个 event_type
        分别计数，claim 的 sequence 语义为该 type 内第几个。
        participants 引用章内 mention_id，返回前做章级 namespace 化。
        """
        etype = payload.get("event_type")
        seq = payload.get("sequence_in_chapter", 1)
        same_type = [ev for ev in self._local_events if ev.get("event_type") == etype]
        if not same_type:
            return []
        # seq 是该类型内第几个（章2：测试 seq1/2/3 → 第1/2/3 个）
        idx = max(0, int(seq or 1) - 1)
        if idx >= len(same_type):
            idx = 0
        return [self._ns(m) for m in same_type[idx].get("participants", [])]

    @property
    def payload(self) -> dict:
        return self._ns_payload(self._claim.payload.model_dump(mode="json"))

    @property
    def observed_chapter_id(self) -> str:
        return self._draft.chapter_id

    @property
    def observed_ordinal(self) -> int:
        return self._draft.chapter_ordinal

    @property
    def evidence(self) -> list[AlignedEvidence]:
        return self._evidences
