"""事件链接落库服务（阶段 09，docs/implementation/09 §3–§4）。

流程：
1. 读取 run 的 event claims（含 participants/evidence/location/ordinal）；
2. EventLinker 生成跨章候选；
3. 候选落 event_links（幂等：claim_version_id 主键）：
   - observed_ordinal = 全部支持证据的最大披露章节（09 §4）；
   - 原因端+结果端证据默认保留（事件自身 evidence 已关联）；
   - claim_status：规则候选默认 unverified（P0：端点证据 ≠ 边因果
     证据）；LinkVerifier 验证通过（目标章原文出现原因引用 + 因果
     连接词）→ supported + 记录 verification_method/evidence（09 §4）；
4. 结果端/原因端可见性由查询层（cutoff）执行，不在此过滤。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from novelcanon.config.hash import stable_config_hash
from novelcanon.events.linker import EventInfo, EventLinker, LinkCandidate
from novelcanon.events.verifier import LinkVerification, LinkVerifier
from novelcanon.evidence.selector import evidence_run_condition
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.ids import claim_version_id, event_link_fact_id
from novelcanon.schemas.memory import EventLinkRecord
from novelcanon.schemas.payloads import EventLinkPayload
from novelcanon.schemas.types import (
    ClaimStatus,
    ClaimType,
    Operation,
    WorldValidKind,
)
from novelcanon.storage.repository import Repository, now_iso


@dataclass
class LinkStats:
    events: int = 0
    candidates: int = 0
    links: int = 0
    unverified: int = 0
    verified: int = 0
    statuses: dict[str, int] = field(default_factory=dict)


class EventLinkService:
    """跨章事件链接：生成候选 + 关系证据验证 + 落库（幂等）。"""

    def __init__(
        self,
        engine: Engine,
        *,
        linker: EventLinker | None = None,
        verifier: LinkVerifier | None = None,
    ) -> None:
        self._engine = engine
        self._repo = Repository(engine)
        self._linker = linker or EventLinker()
        self._verifier = verifier or LinkVerifier()

    # ── 对外 ────────────────────────────────────────────────────

    def link_run(self, run_id: str, book_id: str) -> LinkStats:
        """对某 run 的 event claims 生成候选、验证并落库跨章链接。"""
        stats = LinkStats()
        events = self._load_events(run_id)
        stats.events = len(events)
        candidates = self._linker.generate_candidates(events)
        stats.candidates = len(candidates)
        for cand in candidates:
            written = self._write_link(run_id, book_id, cand)
            if written is None:
                continue
            stats.links += 1
            stats.statuses[written.value] = stats.statuses.get(written.value, 0) + 1
            if written == ClaimStatus.UNVERIFIED:
                stats.unverified += 1
            elif written == ClaimStatus.SUPPORTED:
                stats.verified += 1
        return stats

    # ── 数据读取：event claims → EventInfo ──────────────────────

    def _load_events(self, run_id: str) -> list[EventInfo]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT c.claim_version_id, c.fact_id, c.observed_ordinal,"
                        " c.observed_chapter_id, c.claim_status, c.operation,"
                        " e.event_type, e.summary, e.location_entity_id,"
                        " e.sequence_in_chapter, e.narrative_weight"
                        " FROM event_claims e"
                        " JOIN claims c ON c.claim_version_id = e.claim_version_id"
                        " JOIN claim_observations o ON o.claim_version_id = c.claim_version_id"
                        " WHERE o.extraction_run_id = :r"
                        " ORDER BY c.observed_ordinal, e.sequence_in_chapter"
                    ),
                    {"r": run_id},
                )
                .mappings()
                .fetchall()
            )
        events: list[EventInfo] = []
        for r in rows:
            d = dict(r)
            participants = self._participants_for(d["claim_version_id"])
            if not participants:
                # 无 participants 的事件无法做参与者交集（阶段 07 旧数据），
                # 不参与跨章链接（09 §2 候选阻塞）
                continue
            # 参与者 canonical 化（09 前置：EventLinker 只消费已完成 canonical
            # 映射的数据，08 退出标准）：mention → canonical（经 entity_resolutions）
            participants = self._canonicalize_participants(participants)
            if not participants:
                continue
            evidence_ordinals = self._evidence_ordinals(d["claim_version_id"], run_id)
            evidence_stances = self._evidence_stances(d["claim_version_id"], run_id)
            events.append(
                EventInfo(
                    claim_version_id=d["claim_version_id"],
                    fact_id=d["fact_id"],
                    event_type=d["event_type"],
                    summary=d["summary"],
                    participants=participants,
                    location_entity_id=d["location_entity_id"],
                    observed_ordinal=d["observed_ordinal"],
                    observed_chapter_id=d["observed_chapter_id"],
                    sequence_in_chapter=d["sequence_in_chapter"],
                    narrative_weight=d["narrative_weight"],
                    evidence_ordinals=evidence_ordinals,
                    claim_status=d.get("claim_status", "supported"),
                    operation=d.get("operation", "assert"),
                    evidence_stances=evidence_stances,
                )
            )
        return events

    def _evidence_stances(self, claim_version_id_value: str, run_id: str) -> list[str]:
        """该 claim 在当前 run 下的证据 stance（exact-current-first）。

        P1（十六轮）：事件证据也须按验证 run 隔离——本 run 验证记录
        优先，仅当无本 run 记录时才回退 legacy NULL，不得混入其他 run
        的验证结果。
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT e.evidence_stance FROM claim_evidence e"
                    " WHERE e.claim_version_id = :v AND " + evidence_run_condition()
                ),
                {"v": claim_version_id_value, "vr": run_id},
            ).fetchall()
        return [r[0] for r in rows]

    def _canonicalize_participants(self, participants: list[str]) -> list[str]:
        """把 mention 级参与者投影为 canonical 实体（经 entity_resolutions）。

        参与者本身是 canonical 时原样保留；mention 投影后为空则丢弃。
        """
        out: list[str] = []
        with self._engine.connect() as conn:
            for p in participants:
                row = conn.execute(
                    text("SELECT canonical_id FROM entity_resolutions WHERE mention_id = :m"),
                    {"m": p},
                ).fetchone()
                canonical = row[0] if row is not None else p  # 已是 canonical 时保留
                if canonical not in out:
                    out.append(canonical)
        return out

    def _participants_for(self, event_claim_version_id: str) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT entity_id FROM event_participants WHERE event_claim_version_id = :v"),
                {"v": event_claim_version_id},
            ).fetchall()
        return [r[0] for r in rows]

    def _evidence_ordinals(self, claim_version_id_value: str, run_id: str) -> list[int]:
        """事件支持证据的披露章节（09 §4：observed ordinal = max 证据 ordinal）。

        证据锚定章节 → 该章节 ordinal（chapters.ordinal）。
        P1（十六轮）：仅取当前 run 的验证证据（exact-current-first），
        其他 run 的验证结果不参与本 run 链接的 ordinal 计算。
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT ch.ordinal FROM claim_evidence e"
                    " JOIN chapters ch ON ch.chapter_id = e.chapter_id"
                    " WHERE e.claim_version_id = :v AND " + evidence_run_condition()
                ),
                {"v": claim_version_id_value, "vr": run_id},
            ).fetchall()
        return [r[0] for r in rows]

    # ── 落库（幂等）────────────────────────────────────────────

    def _write_link(self, run_id: str, book_id: str, cand: LinkCandidate) -> ClaimStatus | None:
        """写一条 event_link（幂等：claim_version_id 确定性主键）。"""
        source = cand.source
        target = cand.target
        # observed ordinal = 原因端+结果端证据最大披露章节（09 §4）
        ordinals = source.evidence_ordinals + target.evidence_ordinals
        observed_ordinal = (
            max(ordinals) if ordinals else max(source.observed_ordinal, target.observed_ordinal)
        )
        # 边状态（P0 收紧）：规则层只生成 candidate——端点各自有证据
        # 不等于边有因果证据。LinkVerifier 检查目标章原文是否出现
        # 「原因引用 + 因果连接词」：验证通过 → supported（记录验证
        # 方法与原文 span）；否则保持 unverified。
        verification = self._verify_link(book_id, source, target)
        if verification is not None:
            status = ClaimStatus.SUPPORTED
            verification_method = verification.method
            verification_evidence = json.dumps(
                {
                    "chapter_id": verification.chapter_id,
                    "char_start": verification.char_start,
                    "char_end": verification.char_end,
                    "span_text": verification.span_text,
                    "matched_ref": verification.matched_ref,
                    "matched_connective": verification.matched_connective,
                },
                ensure_ascii=False,
            )
        else:
            status = ClaimStatus.UNVERIFIED
            verification_method = None
            verification_evidence = None
        # primary_evidence_id（09 §4「默认保存原因端和结果端 evidence」）：
        # 因果边不新建 claim_evidence 行（event_links 不是 claims 表事实，
        # FK 约束），而是复用原因端事件的第一个 supports 证据做锚定——
        # 仅作定位参考，不构成边的支持性判定。
        primary_evidence = self._source_evidence(source.claim_version_id, run_id)

        fact_id = event_link_fact_id(
            source.claim_version_id,
            cand.relation_type,
            target.claim_version_id,
        )
        payload = EventLinkPayload(
            source_event_id=source.claim_version_id,
            target_event_id=target.claim_version_id,
            relation_type=cand.relation_type,
        )
        version_key = stable_config_hash(
            {"operation": Operation.ASSERT.value, "payload": payload.model_dump(mode="json")}
        )
        version_id = claim_version_id(fact_id, version_key)
        envelope = ClaimEnvelope(
            fact_id=fact_id,
            claim_version_id=version_id,
            claim_type=ClaimType.EVENT_LINK,
            operation=Operation.ASSERT,
            confidence=cand.confidence,
            claim_status=status,
            observed_chapter_id=target.observed_chapter_id,
            observed_ordinal=observed_ordinal,
            # 图谱边世界有效时间（P1，09 §7）：默认 chapter_proxy——
            # 事件边在「原因端+结果端证据最大披露章节」成立（世界时间
            # 近似 = 披露章节；明确 story_time 的边留后续语义标注）。
            world_valid_kind=WorldValidKind.CHAPTER_PROXY,
            world_valid_from=observed_ordinal,
            world_valid_to=None,
            world_valid_confidence=1.0,
            created_by_run_id=run_id,
            created_at=now_iso(),
            primary_evidence_id=primary_evidence,
        )
        self._repo.write_event_link(
            EventLinkRecord(
                envelope=envelope,
                payload=payload,
                verification_method=verification_method,
                verification_evidence=verification_evidence,
            )
        )
        return status

    def _verify_link(
        self, book_id: str, source: EventInfo, target: EventInfo
    ) -> LinkVerification | None:
        """因果边关系证据验证（09 §4 P0）：目标章原文「原因引用 + 连接词」。

        原因引用 = 原因端参与者 canonical 名 + 原因端 event_type；
        任一与因果连接词同句出现 → 验证通过。
        """
        refs = self._source_refs(source)
        if not refs:
            return None
        target_text = self._repo.chapter_text_for(book_id, target.observed_chapter_id)
        if not target_text:
            return None
        return self._verifier.verify(target.observed_chapter_id, target_text, refs)

    def _source_refs(self, source: EventInfo) -> list[str]:
        """源事件的动作/摘要锚点（P0 收紧：**不含参与者**）。

        候选生成本来就要求参与者交集——目标章出现参与者名是必然的，
        不能作为因果证据。只有源事件的 event_type 标签与 summary 原文
        出现在目标章（且与强连接词同句）才构成关系证据。
        """
        refs: list[str] = []
        if source.event_type and len(source.event_type) >= 2:
            refs.append(source.event_type)
        summary = (source.summary or "").strip()
        if len(summary) >= 2:
            refs.append(summary)
        return refs

    def _source_evidence(self, event_claim_version_id: str, run_id: str) -> str | None:
        """原因端事件的第一个 supports 证据 id（边锚定用，09 §4）。

        P1（十六轮）：只取当前 run 的验证证据（exact-current-first）——
        link 的 primary_evidence_id 必须属于本 run，历史/其他 run 的
        证据不得作为本 run 边的锚定。
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT e.evidence_id FROM claim_evidence e"
                    " WHERE e.claim_version_id = :v AND e.evidence_stance = 'supports'"
                    " AND " + evidence_run_condition() + " ORDER BY e.rowid LIMIT 1"
                ),
                {"v": event_claim_version_id, "vr": run_id},
            ).fetchone()
        return row[0] if row else None
