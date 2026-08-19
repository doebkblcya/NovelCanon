"""阶段 06 开发样本：10 章中文小说 + 人工标注的期望 ExtractionDraftV1。

故事线（青石镇/陆沉线）：别名披露（阿远→陆沉）、状态变化（炼气→筑基，
update）、关系（师徒/未婚夫妻/少主/仇敌）、事件（拜师/身份揭露/离别）、
势力（org join）、伏笔（foreshadowing）、unresolved 提及。

标注即「人工期望」：fake provider 直接返回这些 Draft，等价于完美 LLM，
用于无网络验证「开发样本可无人工修复地完成 Draft 入 staging」。
"""

from __future__ import annotations

from novelcanon.schemas.draft import (
    CauseCandidate,
    ExtractionDraftV1,
    LocalCause,
    LocalEventDraft,
    MentionDraft,
    ProvisionalClaim,
    RefSourceSegment,
    UnresolvedMention,
)
from novelcanon.schemas.payloads import (
    EventPayload,
    ForeshadowPayload,
    OrgPayload,
    RelationPayload,
    StatePayload,
)
from novelcanon.schemas.types import Operation

DEV_BOOK_ID = "book_dev"

# ── 10 章正文（标题, 正文）──────────────────────────────────────

DEV_CHAPTERS: list[tuple[str, str]] = [
    (
        "第一章 青石镇",
        "青石镇的老陈铁匠铺里，阿远正抡着铁锤。他自幼父母双亡，被老陈收留。"
        "镇上人都说，阿远与老陈之女阿杏定了亲，只等来年成婚。",
    ),
    (
        "第二章 回春堂",
        "阿远替阿杏送药到回春堂，坐堂的药老接过药包，忽然盯着阿远看了半晌，"
        "说他有上等根骨。阿远半信半疑，只当药老说笑。",
    ),
    (
        "第三章 拜师",
        "药老正式收阿远为徒，在回春堂设了拜师酒。老陈与阿杏都来道贺，"
        "阿远从此在回春堂学医，也随药老习武。",
    ),
    (
        "第四章 试炼",
        "青云宗开山收徒，阿远随众人上山参加试炼。药老暗中点拨，"
        "阿远一鼓作气通过试炼，境界突破至炼气期。",
    ),
    (
        "第五章 身世",
        "试炼之后，药老才告诉阿远，他本名陆沉，是十五年前遭逢家变、"
        "失踪的陆家少主。阿远怔立良久，只觉世事如梦。",
    ),
    (
        "第六章 仇敌",
        "陆家旧敌赵家闻讯而来，赵家少主赵坤在镇口拦住陆沉，扬言要报当年之仇。"
        "两人当场定下三年之约，届时生死各安天命。",
    ),
    (
        "第七章 突破",
        "回到回春堂，陆沉潜心修炼数月，终于将境界从炼气期提升至筑基期。"
        "药老大喜，说他已经可以出师闯荡。",
    ),
    (
        "第八章 青莲灯",
        "临行前，药老取出一盏古朴的青莲灯交给陆沉，说此灯与陆家旧事有关，"
        "日后或有大用。陆沉郑重收下。",
    ),
    (
        "第九章 入门",
        "陆沉正式加入青云宗外门，与同门弟子一同修炼。赵坤听说后冷笑一声，"
        "说三年之约时青云宗也保不住他。",
    ),
    (
        "第十章 离别",
        "下山那日，阿杏送陆沉到镇口，两人相顾无言。阿杏只说了一句：等你回来。"
        "陆沉点头，转身踏上南行的官道。",
    ),
]


def _span(text: str, needle: str) -> tuple[int, int]:
    idx = text.find(needle)
    assert idx >= 0, f"标注文本不在章中：{needle!r}"
    return idx, idx + len(needle)


def _ref() -> RefSourceSegment:
    # 压缩关闭：pipeline 会覆盖 Draft.ref_source_segments；标注默认 seg_0
    return RefSourceSegment(segment_id="seg_0", char_offset=0, segment_content_hash="x")


def _mentions(text: str, *surfaces: str) -> list[MentionDraft]:
    out = []
    for i, surface in enumerate(surfaces):
        s, e = _span(text, surface)
        out.append(
            MentionDraft(mention_id=f"m_{i}", surface_name=surface, char_start=s, char_end=e)
        )
    return out


def _relation(
    pid: str,
    from_e: str,
    to_e: str,
    rtype: str,
    raw: str,
) -> ProvisionalClaim:
    return ProvisionalClaim(
        provisional_claim_id=pid,
        claim_type="relation",
        payload=RelationPayload(
            from_entity_id=from_e, to_entity_id=to_e, relation_type=rtype, relation_raw=raw
        ),
        ref_source_segment_id="seg_0",
    )


def _state(
    pid: str,
    subject: str,
    field: str,
    value: str,
    *,
    operation: Operation = Operation.ASSERT,
) -> ProvisionalClaim:
    return ProvisionalClaim(
        provisional_claim_id=pid,
        claim_type="state",
        operation=operation,
        payload=StatePayload(
            field=field, value=value, raw_value=value, subject_entity_id=subject
        ),
        ref_source_segment_id="seg_0",
    )


def _event(
    pid: str,
    etype: str,
    summary: str,
    participants: list[str],
    location: str | None = None,
) -> ProvisionalClaim:
    payload: dict = {"event_type": etype, "summary": summary}
    if location is not None:
        payload["location_entity_id"] = location
    return ProvisionalClaim(
        provisional_claim_id=pid,
        claim_type="event",
        payload=EventPayload(**payload),
        ref_source_segment_id="seg_0",
    )


def _org(pid: str, org: str, member: str, role: str) -> ProvisionalClaim:
    return ProvisionalClaim(
        provisional_claim_id=pid,
        claim_type="org",
        payload=OrgPayload(org_entity_id=org, member_entity_id=member, role=role),
        ref_source_segment_id="seg_0",
    )


def _foreshadow(pid: str, anchor: str, related: list[str]) -> ProvisionalClaim:
    return ProvisionalClaim(
        provisional_claim_id=pid,
        claim_type="foreshadowing",
        payload=ForeshadowPayload(clue_anchor=anchor, related_entity_ids=related),
        ref_source_segment_id="seg_0",
    )


def _draft(
    chapter_id: str,
    ordinal: int,
    text: str,
    *,
    mentions: list[MentionDraft],
    claims: list[ProvisionalClaim],
    events: list[LocalEventDraft] | None = None,
    unresolved: list[UnresolvedMention] | None = None,
    causes: list[LocalCause] | None = None,
    candidates: list[CauseCandidate] | None = None,
) -> ExtractionDraftV1:
    return ExtractionDraftV1(
        book_id=DEV_BOOK_ID,
        chapter_id=chapter_id,
        chapter_ordinal=ordinal,
        mentions=mentions,
        local_events=events or [],
        provisional_claims=claims,
        ref_source_segments=[_ref()],
        local_causes=causes or [],
        cause_candidates=candidates or [],
        unresolved=unresolved or [],
    )


def build_dev_drafts(chapter_ids: dict[int, str]) -> list[ExtractionDraftV1]:
    """按章组装人工标注 Draft。

    chapter_ids: ordinal → chapter_id（导入后实际 ID）。
    """
    drafts: list[ExtractionDraftV1] = []
    for ordinal, (_title, text) in enumerate(DEV_CHAPTERS):
        chapter_id = chapter_ids[ordinal]
        if ordinal == 0:
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "阿远", "老陈", "阿杏"),
                claims=[
                    _relation("c1", "阿远", "老陈", "抚养", "被老陈收留"),
                    _relation("c2", "阿远", "阿杏", "未婚夫妻", "阿远与老陈之女阿杏定了亲"),
                ],
            )
        elif ordinal == 1:
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "阿远", "阿杏", "药老"),
                claims=[_relation("c1", "药老", "阿远", "赏识", "有上等根骨")],
            )
        elif ordinal == 2:
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "药老", "阿远", "回春堂"),
                claims=[
                    _event("c1", "拜师", "药老正式收阿远为徒", ["药老", "阿远"], "回春堂"),
                    _relation("c2", "药老", "阿远", "师徒", "正式收阿远为徒"),
                    _org("c3", "回春堂", "阿远", "学徒"),
                ],
                events=[
                    LocalEventDraft(
                        local_event_id="e1",
                        event_type="拜师",
                        summary="药老收阿远为徒",
                        participants=["m_0", "m_1"],
                    )
                ],
            )
        elif ordinal == 3:
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "阿远", "青云宗", "药老"),
                claims=[
                    _state("c1", "阿远", "cultivation_realm", "炼气"),
                    _event("c2", "试炼", "阿远通过青云宗试炼", ["阿远"], "青云宗"),
                ],
                events=[
                    LocalEventDraft(
                        local_event_id="e1",
                        event_type="试炼",
                        summary="阿远通过试炼",
                        participants=["m_0"],
                    )
                ],
            )
        elif ordinal == 4:
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "药老", "阿远", "陆沉", "陆家"),
                claims=[
                    _event("c1", "身份揭露", "药老告知阿远真名陆沉", ["药老", "阿远"]),
                    _relation("c2", "陆沉", "陆家", "少主", "失踪的陆家少主"),
                ],
                events=[
                    LocalEventDraft(
                        local_event_id="e1",
                        event_type="身份揭露",
                        summary="阿远真名陆沉",
                        participants=["m_0", "m_1"],
                    )
                ],
            )
        elif ordinal == 5:
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "陆家", "赵家", "赵坤", "陆沉"),
                claims=[
                    _relation("c1", "陆家", "赵家", "仇敌", "陆家旧敌赵家"),
                    _relation("c2", "陆沉", "赵坤", "仇敌", "定下三年之约"),
                    _event("c3", "约定", "陆沉与赵坤定下三年之约", ["陆沉", "赵坤"]),
                ],
                events=[
                    LocalEventDraft(
                        local_event_id="e1",
                        event_type="约定",
                        summary="三年之约",
                        participants=["m_2", "m_3"],
                    )
                ],
            )
        elif ordinal == 6:
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "陆沉", "回春堂", "药老"),
                claims=[
                    _state(
                        "c1", "陆沉", "cultivation_realm", "筑基",
                        operation=Operation.UPDATE,  # 炼气 → 筑基：update 语义
                    )
                ],
            )
        elif ordinal == 7:
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "药老", "陆沉", "青莲灯", "陆家"),
                claims=[
                    _foreshadow("c1", "青莲灯", ["陆沉", "陆家"]),
                    _relation("c2", "青莲灯", "陆家", "关联", "与陆家旧事有关"),
                ],
            )
        elif ordinal == 8:
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "陆沉", "青云宗", "赵坤"),
                claims=[
                    _org("c1", "青云宗", "陆沉", "外门弟子"),
                    _relation("c2", "陆沉", "青云宗", "同门", "与同门弟子一同修炼"),
                ],
            )
        else:  # ordinal == 9
            draft = _draft(
                chapter_id,
                ordinal,
                text,
                mentions=_mentions(text, "阿杏", "陆沉"),
                claims=[_event("c1", "离别", "阿杏送陆沉离开", ["阿杏", "陆沉"])],
                events=[
                    LocalEventDraft(
                        local_event_id="e1",
                        event_type="离别",
                        summary="阿杏送别陆沉",
                        participants=["m_0", "m_1"],
                    )
                ],
                unresolved=[
                    UnresolvedMention(
                        surface_name="南行",
                        chapter_id=chapter_id,
                        char_start=text.find("南行"),
                        char_end=text.find("南行") + 2,
                        context="转身踏上南行的官道",
                    )
                ],
            )
        drafts.append(draft)
    return drafts
