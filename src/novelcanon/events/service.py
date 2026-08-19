"""事件链接落库服务（阶段 09，docs/implementation/09 §3–§4）。

流程：
1. 读取 run 的 event claims（含 participants/evidence/location/ordinal）；
2. EventLinker 生成跨章候选；
3. 高置信候选落 event_links（幂等：claim_version_id 主键）：
   - observed_ordinal = 全部支持证据的最大披露章节（09 §4）；
   - 原因端+结果端证据默认保留（事件自身 evidence 已关联）；
   - claim_status 聚合：有证据 → supported，无 → unverified（09 §3）；
4. 结果端/原因端可见性由查询层（cutoff）执行，不在此过滤。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from novelcanon.config.hash import stable_config_hash
from novelcanon.events.linker import EventInfo, EventLinker, LinkCandidate
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.ids import claim_version_id, event_link_fact_id
from novelcanon.schemas.memory import EventLinkRecord
from novelcanon.schemas.payloads import EventLinkPayload
from novelcanon.schemas.types import ClaimStatus, Operation
from novelcanon.storage.repository import Repository, now_iso


@dataclass
class LinkStats:
    events: int = 0
    candidates: int = 0
    links: int = 0
    unverified: int = 0
    statuses: dict[str, int] = field(default_factory=dict)


class EventLinkService:
    """跨章事件链接：生成候选 + 落库（幂等）。"""

    def __init__(self, engine: Engine, *, linker: EventLinker | None = None) -> None:
        self._engine = engine
        self._repo = Repository(engine)
        self._linker = linker or EventLinker()

    # ── 对外 ────────────────────────────────────────────────────

    def link_run(self, run_id: str, book_id: str) -> LinkStats:
        """对某 run 的 event claims 生成并落库跨章链接。"""
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
        return stats

    # ── 数据读取：event claims → EventInfo ──────────────────────

    def _load_events(self, run_id: str) -> list[EventInfo]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT c.claim_version_id, c.fact_id, c.observed_ordinal,"
                        " c.observed_chapter_id,"
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
            evidence_ordinals = self._evidence_ordinals(d["claim_version_id"])
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
                )
            )
        return events

    def _canonicalize_participants(self, participants: list[str]) -> list[str]:
        """把 mention 级参与者投影为 canonical 实体（经 entity_resolutions）。

        参与者本身是 canonical 时原样保留；mention 投影后为空则丢弃。
        """
        out: list[str] = []
        with self._engine.connect() as conn:
            for p in participants:
                row = conn.execute(
                    text(
                        "SELECT canonical_id FROM entity_resolutions WHERE mention_id = :m"
                    ),
                    {"m": p},
                ).fetchone()
                if row is not None:
                    canonical = row[0]
                else:
                    canonical = p  # 已是 canonical（或未消歧，保留原样）
                if canonical not in out:
                    out.append(canonical)
        return out

    def _participants_for(self, event_claim_version_id: str) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT entity_id FROM event_participants"
                    " WHERE event_claim_version_id = :v"
                ),
                {"v": event_claim_version_id},
            ).fetchall()
        return [r[0] for r in rows]

    def _evidence_ordinals(self, claim_version_id_value: str) -> list[int]:
        """事件支持证据的披露章节（09 §4：observed ordinal = max 证据 ordinal）。

        证据锚定章节 → 该章节 ordinal（chapters.ordinal）。
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT ch.ordinal FROM claim_evidence e"
                    " JOIN chapters ch ON ch.chapter_id = e.chapter_id"
                    " WHERE e.claim_version_id = :v"
                ),
                {"v": claim_version_id_value},
            ).fetchall()
        return [r[0] for r in rows]

    # ── 落库（幂等）────────────────────────────────────────────

    def _write_link(
        self, run_id: str, book_id: str, cand: LinkCandidate
    ) -> ClaimStatus | None:
        """写一条 event_link（幂等：claim_version_id 确定性主键）。"""
        source = cand.source
        target = cand.target
        # observed ordinal = 原因端+结果端证据最大披露章节（09 §4）
        ordinals = source.evidence_ordinals + target.evidence_ordinals
        observed_ordinal = max(ordinals) if ordinals else max(
            source.observed_ordinal, target.observed_ordinal
        )
        # 证据覆盖：两端都有证据 → supported；否则 unverified（09 §3）
        has_evidence = bool(source.evidence_ordinals and target.evidence_ordinals)
        status = ClaimStatus.SUPPORTED if has_evidence else ClaimStatus.UNVERIFIED

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
            claim_type="event_link",
            operation=Operation.ASSERT,
            confidence=cand.confidence,
            claim_status=status,
            observed_chapter_id=target.observed_chapter_id,
            observed_ordinal=observed_ordinal,
            created_by_run_id=run_id,
            created_at=now_iso(),
        )
        self._repo.write_event_link(
            EventLinkRecord(envelope=envelope, payload=payload)
        )
        return status
