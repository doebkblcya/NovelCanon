"""阶段 05 黄金样例：4 章中文小说 + 人工期望（docs/implementation/05 §黄金样例设计）。

要素覆盖：
- 人物先以别名（小石）出现，第四章披露正式名（林风）；
- 两个相似名称不同实体：林风（林家少主）vs 林锋（散修）；
- 状态值改变：境界 金丹（第 3 章）→ 元婴（第 4 章）；
- 明确一跳关系：学徒 / 未婚夫妻 / 师徒 / 少主；
- 事件及参与者：拜师（第 3 章）、身份揭露（第 4 章）；
- 一条事实被更新：cultivation_realm；
- 精确直接证据 span；
- 截止早期章节必须隐藏：正式名/身份/元婴。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── 黄金原文（章标题 + 正文）──────────────────────────────────

GOLDEN_CHAPTERS: list[tuple[str, str]] = [
    (
        "第一章 少年登场",
        "云雾山下的小镇里，小石正在劈柴。他皮肤黝黑，力气惊人，是镇上铁匠的学徒。"
        "镇上人都说，小石与铁匠之女小荷定了亲，两人青梅竹马。",
    ),
    (
        "第二章 青云宗来客",
        "青云宗的弟子路过小镇，一眼看中小石的根骨，要收他入门。"
        "铁匠大喜，连说小石若能拜入青云宗，便是祖坟冒了青烟。",
    ),
    (
        "第三章 入门拜师",
        "小石拜入青云宗掌门青云子门下，成为青云子的亲传弟子。"
        "入门当日测出灵根，境界直接突破至金丹期。散修林锋也在一旁观礼。",
    ),
    (
        "第四章 身份揭露",
        "众人这才知道，小石真名林风，是二十年前失踪的林家少主。"
        "林风与青云子约定五年后下山寻仇。此时的林风，境界已达元婴。",
    ),
]

# ── canonical 实体（人工映射目标）─────────────────────────────

CANONICAL_ENTITIES: dict[str, str] = {
    "ent_xiaoshi": "林风",  # 小石 → 林风（最终名）
    "ent_tiejian": "铁匠",
    "ent_xiaohe": "小荷",
    "ent_qingyunzi": "青云子",
    "ent_linfeng": "林锋",  # 散修（相似名，非林风）
    "ent_qingyunzong": "青云宗",
    "ent_linjia": "林家",
}

# mention surface → canonical_id（黄金 draft 直接给出映射）
MENTION_MAP: dict[str, str] = {
    # ch1
    "m_xiaoshi_c1": "ent_xiaoshi",
    "m_tiejian_c1": "ent_tiejian",
    "m_xiaohe_c1": "ent_xiaohe",
    # ch2
    "m_qingyunzong_c2": "ent_qingyunzong",
    "m_xiaoshi_c2": "ent_xiaoshi",
    "m_tiejian_c2": "ent_tiejian",
    # ch3
    "m_xiaoshi_c3": "ent_xiaoshi",
    "m_qingyunzi_c3": "ent_qingyunzi",
    "m_linfeng_c3": "ent_linfeng",
    # ch4
    "m_xiaoshi_c4": "ent_xiaoshi",
    "m_linfeng_c4": "ent_xiaoshi",  # 披露正式名 → 同一 canonical
    "m_qingyunzi_c4": "ent_qingyunzi",
    "m_linjia_c4": "ent_linjia",
    "m_linfeng2_c4": "ent_linfeng",  # 散修林锋（第四章再提）
}


@dataclass(frozen=True)
class GoldenEvidence:
    """黄金证据：直接指向原文 span（章内 code point 半开区间）。"""

    chapter_id: str
    char_start: int
    char_end: int
    span_text: str  # 与原文切片必须一致（hash 验证用）


@dataclass(frozen=True)
class GoldenClaim:
    """固定 Draft 中的一条事实 + 直接证据。"""

    claim_type: str
    payload: dict
    observed_chapter_id: str
    observed_ordinal: int
    evidence: GoldenEvidence
    # fact 语义字段（进入 fact_id，§4.3）
    fact_fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GoldenDraft:
    """一章的固定抽取产物。"""

    chapter_id: str
    ordinal: int
    mentions: list[tuple[str, str]]  # (mention_id, surface_name)
    claims: list[GoldenClaim] = field(default_factory=list)


def _span(text: str, needle: str) -> tuple[int, int]:
    """在章文本中定位证据子串（找不到即测试失败）。"""
    idx = text.find(needle)
    assert idx >= 0, f"证据文本不在章中：{needle!r}"
    return idx, idx + len(needle)


def make_golden_drafts(
    chapter_ids: dict[int, str], chapter_texts: dict[int, str]
) -> list[GoldenDraft]:
    """按章组装固定 Draft（证据 span 从原文精确定位）。

    chapter_ids/_texts: ordinal → chapter_id / 章文本。
    """
    drafts: list[GoldenDraft] = []
    for ordinal, chapter_id in sorted(chapter_ids.items()):
        text = chapter_texts[ordinal]
        claims: list[GoldenClaim] = []
        if ordinal == 0:
            s, e = _span(text, "是镇上铁匠的学徒")
            claims.append(
                GoldenClaim(
                    claim_type="relation",
                    payload={
                        "from_entity_id": "ent_xiaoshi",
                        "to_entity_id": "ent_tiejian",
                        "relation_type": "学徒",
                        "relation_raw": "是镇上铁匠的学徒",
                    },
                    fact_fields={
                        "from_entity_id": "ent_xiaoshi",
                        "relation_type": "学徒",
                        "to_entity_id": "ent_tiejian",
                    },
                    observed_chapter_id=chapter_id,
                    observed_ordinal=ordinal,
                    evidence=GoldenEvidence(chapter_id, s, e, text[s:e]),
                )
            )
            s, e = _span(text, "小石与铁匠之女小荷定了亲")
            claims.append(
                GoldenClaim(
                    claim_type="relation",
                    payload={
                        "from_entity_id": "ent_xiaoshi",
                        "to_entity_id": "ent_xiaohe",
                        "relation_type": "未婚夫妻",
                        "relation_raw": "小石与铁匠之女小荷定了亲",
                    },
                    fact_fields={
                        "from_entity_id": "ent_xiaoshi",
                        "relation_type": "未婚夫妻",
                        "to_entity_id": "ent_xiaohe",
                    },
                    observed_chapter_id=chapter_id,
                    observed_ordinal=ordinal,
                    evidence=GoldenEvidence(chapter_id, s, e, text[s:e]),
                )
            )
        elif ordinal == 2:
            s, e = _span(text, "小石拜入青云宗掌门青云子门下")
            claims.append(
                GoldenClaim(
                    claim_type="event",
                    payload={
                        "event_type": "拜师",
                        "summary": "小石拜入青云子门下",
                        "location_entity_id": "ent_qingyunzong",
                    },
                    fact_fields={
                        "event_type": "拜师",
                        "participants": ["ent_xiaoshi", "ent_qingyunzi"],
                        "location_entity_id": "ent_qingyunzong",
                        "chapter_id": chapter_id,
                        "sequence_in_chapter": 1,
                    },
                    observed_chapter_id=chapter_id,
                    observed_ordinal=ordinal,
                    evidence=GoldenEvidence(chapter_id, s, e, text[s:e]),
                )
            )
            s, e = _span(text, "成为青云子的亲传弟子")
            claims.append(
                GoldenClaim(
                    claim_type="relation",
                    payload={
                        "from_entity_id": "ent_xiaoshi",
                        "to_entity_id": "ent_qingyunzi",
                        "relation_type": "师徒",
                        "relation_raw": "成为青云子的亲传弟子",
                    },
                    fact_fields={
                        "from_entity_id": "ent_xiaoshi",
                        "relation_type": "师徒",
                        "to_entity_id": "ent_qingyunzi",
                    },
                    observed_chapter_id=chapter_id,
                    observed_ordinal=ordinal,
                    evidence=GoldenEvidence(chapter_id, s, e, text[s:e]),
                )
            )
            s, e = _span(text, "境界直接突破至金丹期")
            claims.append(
                GoldenClaim(
                    claim_type="state",
                    payload={"field": "cultivation_realm", "value": "金丹", "raw_value": "金丹"},
                    fact_fields={"subject_entity_id": "ent_xiaoshi", "field": "cultivation_realm"},
                    observed_chapter_id=chapter_id,
                    observed_ordinal=ordinal,
                    evidence=GoldenEvidence(chapter_id, s, e, text[s:e]),
                )
            )
        elif ordinal == 3:
            s, e = _span(text, "小石真名林风")
            claims.append(
                GoldenClaim(
                    claim_type="event",
                    payload={
                        "event_type": "身份揭露",
                        "summary": "小石真名林风",
                        "location_entity_id": None,
                    },
                    fact_fields={
                        "event_type": "身份揭露",
                        "participants": ["ent_xiaoshi"],
                        "location_entity_id": None,
                        "chapter_id": chapter_id,
                        "sequence_in_chapter": 1,
                    },
                    observed_chapter_id=chapter_id,
                    observed_ordinal=ordinal,
                    evidence=GoldenEvidence(chapter_id, s, e, text[s:e]),
                )
            )
            s, e = _span(text, "是二十年前失踪的林家少主")
            claims.append(
                GoldenClaim(
                    claim_type="relation",
                    payload={
                        "from_entity_id": "ent_xiaoshi",
                        "to_entity_id": "ent_linjia",
                        "relation_type": "少主",
                        "relation_raw": "是二十年前失踪的林家少主",
                    },
                    fact_fields={
                        "from_entity_id": "ent_xiaoshi",
                        "relation_type": "少主",
                        "to_entity_id": "ent_linjia",
                    },
                    observed_chapter_id=chapter_id,
                    observed_ordinal=ordinal,
                    evidence=GoldenEvidence(chapter_id, s, e, text[s:e]),
                )
            )
            s, e = _span(text, "境界已达元婴")
            claims.append(
                GoldenClaim(
                    claim_type="state",
                    payload={"field": "cultivation_realm", "value": "元婴", "raw_value": "元婴"},
                    fact_fields={"subject_entity_id": "ent_xiaoshi", "field": "cultivation_realm"},
                    observed_chapter_id=chapter_id,
                    observed_ordinal=ordinal,
                    evidence=GoldenEvidence(chapter_id, s, e, text[s:e]),
                )
            )

        mentions: list[tuple[str, str]] = []
        if ordinal == 0:
            mentions = [("m_xiaoshi_c1", "小石"), ("m_tiejian_c1", "铁匠"), ("m_xiaohe_c1", "小荷")]
        elif ordinal == 1:
            mentions = [
                ("m_qingyunzong_c2", "青云宗"),
                ("m_xiaoshi_c2", "小石"),
                ("m_tiejian_c2", "铁匠"),
            ]
        elif ordinal == 2:
            mentions = [
                ("m_xiaoshi_c3", "小石"),
                ("m_qingyunzi_c3", "青云子"),
                ("m_linfeng_c3", "林锋"),
            ]
        elif ordinal == 3:
            mentions = [
                ("m_xiaoshi_c4", "小石"),
                ("m_linfeng_c4", "林风"),
                ("m_qingyunzi_c4", "青云子"),
                ("m_linjia_c4", "林家"),
                ("m_linfeng2_c4", "林锋"),
            ]

        drafts.append(
            GoldenDraft(chapter_id=chapter_id, ordinal=ordinal, mentions=mentions, claims=claims)
        )
    return drafts
