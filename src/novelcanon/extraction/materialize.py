"""固定 Draft 落库（阶段 05 最小闭环；阶段 06 复用同一 materialize 契约）。

把黄金/Map 抽取产物转换为正式数据：
- mention → canonical entity（upsert）+ mention 行 + alias claim（披露顺序）；
- provisional claim → 稳定 fact/version ID → claims + 类型子表；
- 直接证据：原文切片 → span hash 100% 复现校验 → claim_evidence；
- 证据聚合 → claim_status（supports → supported，其余不进默认回答）。

全程幂等：同 payload 复用版本，仅新增 observation（§4.3）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import Engine

from novelcanon.config.hash import stable_config_hash
from novelcanon.ingestion.normalize import sha256
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.ids import (
    alias_fact_id,
    claim_version_id,
    event_fact_id,
    event_link_fact_id,
    evidence_id,
    foreshadow_fact_id,
    org_fact_id,
    relation_fact_id,
    state_fact_id,
    term_definition_fact_id,
)
from novelcanon.schemas.memory import AliasClaim, EntityRecord, EvidenceRecord
from novelcanon.schemas.payloads import (
    EventPayload,
    ForeshadowPayload,
    OrgPayload,
    RelationPayload,
    StatePayload,
    TermDefinitionPayload,
)
from novelcanon.schemas.types import (
    ClaimType,
    EntityTier,
    EvidenceStance,
    EvidenceType,
    Operation,
)
from novelcanon.storage.evidence_policy import aggregate_claim_status
from novelcanon.storage.repository import Repository, now_iso

_PAYLOAD_MODELS = {
    "relation": RelationPayload,
    "event": EventPayload,
    "state": StatePayload,
    "org": OrgPayload,
    "foreshadowing": ForeshadowPayload,
    "term_definition": TermDefinitionPayload,
}


@dataclass
class MaterializeStats:
    entities: int = 0
    mentions: int = 0
    aliases: int = 0
    claims: int = 0
    new_claims: int = 0
    evidence: int = 0
    verified_evidence: int = 0


@dataclass(frozen=True)
class GoldenEvidenceLike(Protocol):
    chapter_id: str
    char_start: int
    char_end: int
    span_text: str


class GoldenClaimLike(Protocol):
    claim_type: str
    operation: Operation
    fact_fields: Mapping[str, object]
    payload: dict
    observed_chapter_id: str
    observed_ordinal: int
    evidence: GoldenEvidenceLike


@dataclass(frozen=True)
class GoldenDraftLike:
    """materialize 输入的最小契约（黄金 draft / 未来 Map Draft 的适配面）。

    - mentions: (mention_id, surface_name)，mention_id 必须稳定（幂等主键，
      禁止调用方每次生成随机 ID）；
    - entity_tiers: canonical_id → tier（默认 MINOR，不硬编码测试实体）。
    """

    chapter_id: str
    ordinal: int
    mentions: list[tuple[str, str]] = field(default_factory=list)
    claims: list[GoldenClaimLike] = field(default_factory=list)
    entity_tiers: Mapping[str, EntityTier] = field(default_factory=dict)


def _fact_id_for(claim_type: str, fact_fields: Mapping[str, object]) -> str:
    def participants() -> list[str]:
        raw = fact_fields.get("participants")
        return [str(x) for x in raw] if isinstance(raw, list) else []

    if claim_type == "relation":
        return relation_fact_id(
            str(fact_fields["from_entity_id"]),
            str(fact_fields["relation_type"]),
            str(fact_fields["to_entity_id"]),
        )
    if claim_type == "event":
        return event_fact_id(
            str(fact_fields["event_type"]),
            participants(),
            str(fact_fields.get("location_entity_id"))
            if fact_fields.get("location_entity_id")
            else None,
            str(fact_fields["chapter_id"]),
            int(str(fact_fields.get("sequence_in_chapter") or 0)),
        )
    if claim_type == "state":
        return state_fact_id(str(fact_fields["subject_entity_id"]), str(fact_fields["field"]))
    if claim_type == "org":
        return org_fact_id(
            str(fact_fields["org_entity_id"]),
            str(fact_fields["member_entity_id"]),
            str(fact_fields.get("role", "")),
        )
    if claim_type == "foreshadowing":
        return foreshadow_fact_id(str(fact_fields["clue_anchor"]), participants())
    if claim_type == "event_link":
        from novelcanon.schemas.types import EventLinkType

        return event_link_fact_id(
            str(fact_fields["source_event_id"]),
            EventLinkType(str(fact_fields["relation_type"])),
            str(fact_fields["target_event_id"]),
        )
    if claim_type == "term_definition":
        return term_definition_fact_id(str(fact_fields["term_id"]))
    raise ValueError(f"未知 claim_type: {claim_type}")


def materialize_draft(
    engine: Engine,
    *,
    run_id: str,
    book_id: str,
    draft: GoldenDraftLike,
    canonical_map: Mapping[str, str],
    chapter_text: str,
    repo: Repository | None = None,
) -> MaterializeStats:
    """把一章固定 Draft 落库（幂等）。

    canonical_map: mention_id → canonical_id；chapter_text: 该章规范化文本
    （证据 span hash 校验用）。
    """
    repo = repo or Repository(engine)
    # 写入边界校验（P1）：run / 章节必须属于该书，防止把书 B 的章节挂到
    # 书 A 的 active run 上（查询层按 run.book_id 过滤会把错挂数据投影成 A）。
    repo.ensure_run_belongs_to_book(run_id, book_id)
    repo.ensure_chapter_belongs_to_book(draft.chapter_id, book_id)
    stats = MaterializeStats()

    # ── entities / mentions / aliases ──────────────────────────
    for mention_id, surface in draft.mentions:
        canonical_id = canonical_map[mention_id]
        if not repo.get_entity(canonical_id):
            repo.upsert_entity(
                EntityRecord(
                    canonical_id=canonical_id,
                    canonical_name=surface,  # 首次披露 surface 为初始名
                    tier=draft.entity_tiers.get(canonical_id, EntityTier.MINOR),
                    created_by_run_id=run_id,
                )
            )
            stats.entities += 1
        # mention_id 必须来自输入（稳定幂等主键）；随机 ID 会破坏幂等
        repo.write_mention(
            mention_id, draft.chapter_id, surface, run_id, canonical_id=canonical_id
        )
        stats.mentions += 1
        res = repo.write_alias(
            AliasClaim(
                alias_fact_id=alias_fact_id(canonical_id, surface),
                claim_version_id="",
                canonical_id=canonical_id,
                surface_name=surface,
                observed_ordinal=draft.ordinal,
                observed_chapter_id=draft.chapter_id,
                created_by_run_id=run_id,
                created_at=now_iso(),
            )
        )
        if res.is_new:
            stats.aliases += 1

    # ── claims + evidence ──────────────────────────────────────
    for claim in draft.claims:
        ctype = str(claim.claim_type)
        # Map 契约：claim 只能描述本章（observed_chapter_id == draft.chapter_id），
        # 证据已要求归属 observed_chapter_id，因此 claim/evidence 都落在本书内。
        if claim.observed_chapter_id != draft.chapter_id:
            raise ValueError(
                f"claim.observed_chapter_id {claim.observed_chapter_id} 与本章"
                f" {draft.chapter_id} 不一致"
            )
        fact_id = _fact_id_for(ctype, claim.fact_fields)
        payload_dict = dict(claim.payload)
        if ctype == "state" and "subject_entity_id" not in payload_dict:
            payload_dict["subject_entity_id"] = claim.fact_fields["subject_entity_id"]
        payload_model = _PAYLOAD_MODELS[ctype](**payload_dict)
        # version 键必须与 write_claim 内部一致（模型序列化含默认字段）
        canonical_payload = payload_model.model_dump(mode="json")
        version_key = stable_config_hash(
            {"operation": claim.operation.value, "payload": canonical_payload}
        )
        version_id = claim_version_id(fact_id, version_key)

        env = ClaimEnvelope(
            fact_id=fact_id,
            claim_version_id=version_id,
            claim_type=ClaimType(ctype),
            operation=claim.operation,
            observed_chapter_id=claim.observed_chapter_id,
            observed_ordinal=claim.observed_ordinal,
            created_by_run_id=run_id,
            created_at=now_iso(),
        )
        write_result = repo.write_claim(env, payload_model)
        if write_result.is_new:
            stats.new_claims += 1
        stats.claims += 1

        if ctype == "event":
            raw_participants = claim.fact_fields.get("participants")
            participants_list = (
                [str(x) for x in raw_participants] if isinstance(raw_participants, list) else []
            )
            for participant in participants_list:
                repo.add_event_participant(version_id, str(participant))

        # ── 直接证据：原文切片 → span hash 校验（100% 复现）──
        ev = claim.evidence
        if ev.chapter_id != claim.observed_chapter_id:
            raise AssertionError(
                f"证据章节 {ev.chapter_id} 与 claim 章节 {claim.observed_chapter_id} 不一致"
            )
        if not 0 <= ev.char_start < ev.char_end <= len(chapter_text):
            raise AssertionError(
                f"证据 span [{ev.char_start},{ev.char_end}) 越界（章长 {len(chapter_text)}）"
            )
        span = chapter_text[ev.char_start : ev.char_end]
        if sha256(span) != sha256(ev.span_text):
            raise AssertionError(
                f"证据 span 无法复现：{claim.observed_chapter_id} [{ev.char_start},{ev.char_end})"
            )
        eid = evidence_id(version_id, ev.chapter_id, ev.char_start, ev.char_end, sha256(span))
        if repo.write_evidence(
            EvidenceRecord(
                evidence_id=eid,
                claim_version_id=version_id,
                evidence_stance=EvidenceStance.SUPPORTS,
                evidence_type=EvidenceType.DIRECT,
                chapter_id=ev.chapter_id,
                char_start=ev.char_start,
                char_end=ev.char_end,
                span_hash=sha256(span),
                literal_match_rate=1.0,
                verification_method="hash-exact",
            )
        ):
            stats.evidence += 1
            stats.verified_evidence += 1

        # ── 聚合 claim 状态（supports → supported）────────────
        status = aggregate_claim_status([EvidenceStance.SUPPORTS])
        repo.set_claim_status(version_id, status.value)

    return stats
