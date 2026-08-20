"""EventLinker（阶段 09，docs/implementation/09 §2–§3）。

跨章事件因果链接：
1. 候选阻塞（§2）：参与者交集（canonical 化后）、事件类型兼容性、
   时间窗口（source ordinal < target ordinal）、叙事距离；
2. 高置信规则（§3）：确定性规则直接生成（同参与者跨章事件），
   低置信候选留待受约束语义判定（阶段 09 提供规则层 + 阈值）；
3. relation_type 只允许 causes / enables / prevents（表约束已强制）。

因果语义（§4）：
- 默认保留原因端和结果端 evidence（事件各自的证据已落库）；
- event link 的 observed ordinal = 支持证据的最大披露章节；
- 无支持证据的链接保持 unverified（不进入默认因果回答）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from novelcanon.schemas.types import EventLinkType

# 因果链接置信度（规则层）
_CONFIDENCE_ENABLES = 0.6  # 参与者交集 + 时间先后的弱因果（使能）
_CONFIDENCE_CAUSES = 0.8  # 同参与者 + 同地点 + 时间先后的强因果


@dataclass(frozen=True)
class EventInfo:
    """全局事件标识（09 §1）：event fact_id 的稳定输入。"""

    claim_version_id: str
    fact_id: str
    event_type: str
    summary: str
    participants: list[str]  # canonical 实体 ID（已消歧）
    location_entity_id: str | None
    observed_ordinal: int
    observed_chapter_id: str
    sequence_in_chapter: int
    narrative_weight: float = 0.5
    evidence_ordinals: list[int] = field(default_factory=list)
    # P0 修复：事件自身状态与证据立场——因果边只消费 supported、
    # 非 retract、证据为 supports 的事件（09 §4）
    claim_status: str = "supported"
    operation: str = "assert"
    evidence_stances: list[str] = field(default_factory=list)

    @property
    def is_supported(self) -> bool:
        return (
            self.claim_status == "supported"
            and self.operation != "retract"
            and bool(self.evidence_stances)
            and all(s == "supports" for s in self.evidence_stances)
        )


@dataclass(frozen=True)
class LinkCandidate:
    """一条跨章候选链接（待验证/落库）。"""

    source: EventInfo
    target: EventInfo
    relation_type: EventLinkType
    confidence: float
    reason: str


class EventLinker:
    """确定性事件链接器（09 §3 高置信规则层）。"""

    def __init__(self, max_ordinal_gap: int = 100) -> None:
        self._max_gap = max_ordinal_gap

    def generate_candidates(self, events: list[EventInfo]) -> list[LinkCandidate]:
        """跨章因果候选（§2 候选阻塞）。

        规则：
        - 只消费 supported 事件（P0：端点事件自身 supported、非 retract、
          证据为 supports，否则不能成为因果边端点）；
        - 参与者交集非空（canonical 化后）；
        - source.ordinal < target.ordinal（时间窗口）；
        - ordinal 差距不超过 max_ordinal_gap；
        - 同地点 + 同参与者 → causes（强）；仅参与者交集 → enables（弱）；
        - 排除同章内自链（跨章链接，local causes 已在 Map 阶段处理）。
        """
        candidates: list[LinkCandidate] = []
        supported = [ev for ev in events if ev.is_supported]
        by_participant: dict[str, list[EventInfo]] = {}
        for ev in supported:
            for p in ev.participants:
                by_participant.setdefault(p, []).append(ev)

        seen: set[tuple[str, str]] = set()
        for evs in by_participant.values():
            for src in evs:
                for tgt in evs:
                    if src is tgt:
                        continue
                    if src.observed_ordinal >= tgt.observed_ordinal:
                        continue
                    gap = tgt.observed_ordinal - src.observed_ordinal
                    if gap > self._max_gap:
                        continue
                    key = (src.fact_id, tgt.fact_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    # 同地点 → 强因果（拜师→突破 同在山门）；否则弱使能
                    same_location = bool(
                        src.location_entity_id and src.location_entity_id == tgt.location_entity_id
                    )
                    if same_location:
                        rtype = EventLinkType.CAUSES
                        conf = _CONFIDENCE_CAUSES
                        reason = "same-participant-same-location"
                    else:
                        rtype = EventLinkType.ENABLES
                        conf = _CONFIDENCE_ENABLES
                        reason = "same-participant-time-order"
                    candidates.append(
                        LinkCandidate(
                            source=src,
                            target=tgt,
                            relation_type=rtype,
                            confidence=conf,
                            reason=reason,
                        )
                    )
        # 按置信度降序（高置信先落库）
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates

    @staticmethod
    def _type_compatible(src: EventInfo, tgt: EventInfo) -> bool:
        """事件类型兼容性（§2）：同类型或类型文本有交集。"""
        return src.event_type == tgt.event_type or bool(set(src.event_type) & set(tgt.event_type))
