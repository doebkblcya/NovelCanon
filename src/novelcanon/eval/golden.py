"""评测黄金集（阶段 11 P1，docs/implementation/11 §P1）。

评测集在查看正式评测结果前冻结（11 前置条件「验收阈值在查看正式评测
结果前写入测试配置」）——黄金 QA 的期望引用章节、实体合并对、事实与
因果边均为人工标注，不含系统输出。

正式 P1/P2 评测（30 万字短篇 / 200–300 章样本）通过 JSON 文件加载：
CLI `novelcanon pilot --golden golden.json`，校验 book_id、章节范围与
书内容 hash（阶段 11 复审 P0：不得对任意 book 误用 fixture 黄金集）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

GOLDEN_SCHEMA_VERSION = "golden-v1"


@dataclass(frozen=True)
class GoldenQA:
    """黄金 QA：问题 + 期望引用的披露章节（ordinal 集合）。"""

    question: str
    chapter_ordinals: tuple[int, ...]
    query_type: str = ""


@dataclass(frozen=True)
class GoldenEntityMerge:
    """黄金实体合并对：同一 canonical 下的全部表面名。"""

    canonical: str
    surfaces: tuple[str, ...]


@dataclass(frozen=True)
class GoldenFact:
    """黄金事实：claim_type + 语义描述 + 期望披露章节 + 直接证据 span。

    evidence_span：该事实的直接证据原文切片（压缩评测中用于验证事实
    在压缩文本中可重新锚定；与 claim_evidence 同源）。
    """

    claim_type: str
    description: str  # 人类可读期望（如「林风 的 境界 = 元婴」）
    chapter_ordinal: int
    evidence_span: str = ""


@dataclass(frozen=True)
class GoldenCausal:
    """黄金因果边：源事件摘要 → 目标事件摘要。"""

    source: str
    target: str


@dataclass(frozen=True)
class GoldenClaimSpec:
    """黄金 claim 结构化定义（压缩评测「真实抽取」用）。

    与系统 materialize 契约对齐：压缩文本上重新定位证据 span 后，按此
    规格真实落库（走抽取/证据 hash 校验/激活全链路），再对压缩书查询。
    """

    claim_type: str
    payload: dict
    fact_fields: dict
    observed_ordinal: int
    operation: str = "assert"
    evidence_span: str = ""


@dataclass(frozen=True)
class GoldenSet:
    """一本书的完整评测黄金集（冻结）。

    - qas / entity_merges / facts / causals：人工标注；
    - evidence_spans：人工冻结的**直接证据原文 span**（与系统
      claim_evidence.span_hash 同源，用于验证 100% 证据 hash 复现）；
    - claims：黄金 claim 的结构化定义（压缩路线真实重抽取的输入）；
    - core_canonicals：核心实体（P1「核心/全部实体合并 F1」分层用）；
    - book_content_hash：书级 normalized_text sha256（文件加载时校验
      黄金集与目标书内容一致，防错配）。
    """

    book_id: str
    qas: list[GoldenQA] = field(default_factory=list)
    entity_merges: list[GoldenEntityMerge] = field(default_factory=list)
    facts: list[GoldenFact] = field(default_factory=list)
    causals: list[GoldenCausal] = field(default_factory=list)
    evidence_spans: list[str] = field(default_factory=list)
    core_canonicals: frozenset[str] = frozenset()
    claims: list[GoldenClaimSpec] = field(default_factory=list)
    book_content_hash: str = ""


def golden_set_to_dict(g: GoldenSet) -> dict:
    """序列化（JSON 可写；含 schema 版本，加载时校验）。"""
    return {
        "schema_version": GOLDEN_SCHEMA_VERSION,
        "book_id": g.book_id,
        "book_content_hash": g.book_content_hash,
        "qas": [
            {
                "question": q.question,
                "chapter_ordinals": list(q.chapter_ordinals),
                "query_type": q.query_type,
            }
            for q in g.qas
        ],
        "entity_merges": [
            {"canonical": m.canonical, "surfaces": list(m.surfaces)} for m in g.entity_merges
        ],
        "facts": [
            {
                "claim_type": f.claim_type,
                "description": f.description,
                "chapter_ordinal": f.chapter_ordinal,
                "evidence_span": f.evidence_span,
            }
            for f in g.facts
        ],
        "causals": [{"source": c.source, "target": c.target} for c in g.causals],
        "evidence_spans": list(g.evidence_spans),
        "core_canonicals": sorted(g.core_canonicals),
        "claims": [
            {
                "claim_type": c.claim_type,
                "payload": c.payload,
                "fact_fields": c.fact_fields,
                "observed_ordinal": c.observed_ordinal,
                "operation": c.operation,
                "evidence_span": c.evidence_span,
            }
            for c in g.claims
        ],
    }


def golden_set_from_dict(data: dict) -> GoldenSet:
    """反序列化 + schema 校验（版本不符 / 缺关键字段直接报错）。"""
    if data.get("schema_version") != GOLDEN_SCHEMA_VERSION:
        raise ValueError(
            f"黄金集 schema 版本不符：{data.get('schema_version')!r} != {GOLDEN_SCHEMA_VERSION!r}"
        )
    book_id = data.get("book_id")
    if not isinstance(book_id, str) or not book_id:
        raise ValueError("黄金集缺少合法 book_id")
    return GoldenSet(
        book_id=book_id,
        book_content_hash=str(data.get("book_content_hash") or ""),
        qas=[
            GoldenQA(
                question=str(q["question"]),
                chapter_ordinals=tuple(int(o) for o in q.get("chapter_ordinals", [])),
                query_type=str(q.get("query_type") or ""),
            )
            for q in data.get("qas", [])
        ],
        entity_merges=[
            GoldenEntityMerge(
                canonical=str(m["canonical"]),
                surfaces=tuple(str(s) for s in m.get("surfaces", [])),
            )
            for m in data.get("entity_merges", [])
        ],
        facts=[
            GoldenFact(
                claim_type=str(f["claim_type"]),
                description=str(f["description"]),
                chapter_ordinal=int(f["chapter_ordinal"]),
                evidence_span=str(f.get("evidence_span") or ""),
            )
            for f in data.get("facts", [])
        ],
        causals=[
            GoldenCausal(source=str(c["source"]), target=str(c["target"]))
            for c in data.get("causals", [])
        ],
        evidence_spans=[str(s) for s in data.get("evidence_spans", [])],
        core_canonicals=frozenset(str(c) for c in data.get("core_canonicals", [])),
        claims=[
            GoldenClaimSpec(
                claim_type=str(c["claim_type"]),
                payload=dict(c.get("payload") or {}),
                fact_fields=dict(c.get("fact_fields") or {}),
                observed_ordinal=int(c["observed_ordinal"]),
                operation=str(c.get("operation") or "assert"),
                evidence_span=str(c.get("evidence_span") or ""),
            )
            for c in data.get("claims", [])
        ],
    )


def golden_set_from_file(path: Path) -> GoldenSet:
    """从 JSON 文件加载冻结黄金集（正式 P1/P2 评测入口）。"""
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    return golden_set_from_dict(data)


# ── 阶段 05 黄金样例的评测级黄金（tests/golden_data.py GOLDEN_CHAPTERS）──


def golden_set_from_chapters(
    book_id: str,
    *,
    include_qas: bool = True,
    include_merges: bool = True,
    include_facts: bool = True,
    include_causals: bool = True,
    include_evidence: bool = True,
) -> GoldenSet:
    """由 4 章黄金样例构造评测黄金集（Pilot fixture 用）。

    期望引用章节为 ordinal（0 基，与系统一致）。证据 span 与
    tests/golden_data.py make_golden_drafts 的直接证据原文逐字一致
    （hash 复现验证依赖精确切片）。
    """
    qas: list[GoldenQA] = []
    if include_qas:
        qas = [
            GoldenQA("小石的境界是什么", (2, 3), "entity_state"),
            GoldenQA("小石和铁匠是什么关系", (0,), "relation"),
            GoldenQA("小石拜入谁的门下", (2,), "relation"),
            GoldenQA("小石的真名是什么", (3,), "entity_state"),
            GoldenQA("青云宗来客做了什么", (1,), "chapter_graph"),
        ]
    merges: list[GoldenEntityMerge] = []
    if include_merges:
        merges = [
            GoldenEntityMerge("ent_xiaoshi", ("小石", "林风")),
            GoldenEntityMerge("ent_tiejian", ("铁匠",)),
            GoldenEntityMerge("ent_xiaohe", ("小荷",)),
            GoldenEntityMerge("ent_qingyunzi", ("青云子",)),
            GoldenEntityMerge("ent_linfeng", ("林锋",)),
        ]
    facts: list[GoldenFact] = []
    if include_facts:
        # description 与 pilot._predict_facts 的输出格式对齐
        # （state：{subject} 的 {field} = {value}；relation：
        #  {from} —[{type}]→ {to}；event：[{type}] {summary}）
        # 注意：state 只列**当前版本**（金丹→元婴 为同一 fact 的 UPDATE，
        # 当前版本是元婴；历史版本由关系/状态演变评测单独覆盖）。
        # evidence_span 与 make_golden_drafts 的直接证据原文逐字一致
        # （压缩评测按证据 span 重新锚定事实）。
        facts = [
            GoldenFact("relation", "ent_xiaoshi —[学徒]→ ent_tiejian", 0, "是镇上铁匠的学徒"),
            GoldenFact(
                "relation", "ent_xiaoshi —[未婚夫妻]→ ent_xiaohe", 0, "小石与铁匠之女小荷定了亲"
            ),
            GoldenFact("relation", "ent_xiaoshi —[师徒]→ ent_qingyunzi", 2, "成为青云子的亲传弟子"),
            GoldenFact(
                "relation", "ent_xiaoshi —[少主]→ ent_linjia", 3, "是二十年前失踪的林家少主"
            ),
            GoldenFact("state", "ent_xiaoshi 的 cultivation_realm = 元婴", 3, "境界已达元婴"),
            GoldenFact("event", "[拜师] 小石拜入青云子门下", 2, "小石拜入青云宗掌门青云子门下"),
            GoldenFact("event", "[身份揭露] 小石真名林风", 3, "小石真名林风"),
            GoldenFact("event", "[境界突破] 境界直接突破至金丹期", 2, "境界直接突破至金丹期"),
        ]
    causals: list[GoldenCausal] = []
    if include_causals:
        # 人工标注的因果边（fixture 原文：因拜师入门 → 当日突破至金丹期）；
        # 与 golden draft 中 拜师/境界突破 两事件的 summary 逐字一致
        # （Pilot 会以 supported 验证边落库，验证因果 precision）。
        causals = [
            GoldenCausal("小石拜入青云子门下", "境界直接突破至金丹期"),
        ]
    evidence_spans: list[str] = []
    if include_evidence:
        # 与 make_golden_drafts 的直接证据原文逐字一致（hash 复现要求精确切片）
        evidence_spans = [
            "是镇上铁匠的学徒",
            "小石与铁匠之女小荷定了亲",
            "小石拜入青云宗掌门青云子门下",
            "成为青云子的亲传弟子",
            "境界直接突破至金丹期",
            "小石真名林风",
            "是二十年前失踪的林家少主",
            "境界已达元婴",
        ]
    return GoldenSet(
        book_id=book_id,
        qas=qas,
        entity_merges=merges,
        facts=facts,
        causals=causals,
        evidence_spans=evidence_spans,
        # 黄金 draft 仅 ent_xiaoshi 标注 CORE（tests/golden_data.py）
        core_canonicals=frozenset({"ent_xiaoshi"}),
        claims=_fixture_claims(),
    )


def _fixture_claims() -> list[GoldenClaimSpec]:
    """4 章黄金样例的结构化 claims（与 tests/golden_data.py 对齐）。

    压缩路线「真实抽取」输入：压缩文本上重定位证据 span 后按此规格
    重新 materialize。event 的 fact_fields 不含 chapter_id（由重抽取
    适配器按 observed_chapter_id 补全）。
    """
    return [
        GoldenClaimSpec(
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
            observed_ordinal=0,
            evidence_span="是镇上铁匠的学徒",
        ),
        GoldenClaimSpec(
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
            observed_ordinal=0,
            evidence_span="小石与铁匠之女小荷定了亲",
        ),
        GoldenClaimSpec(
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
                "sequence_in_chapter": 1,
            },
            observed_ordinal=2,
            evidence_span="小石拜入青云宗掌门青云子门下",
        ),
        GoldenClaimSpec(
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
            observed_ordinal=2,
            evidence_span="成为青云子的亲传弟子",
        ),
        GoldenClaimSpec(
            claim_type="state",
            payload={"field": "cultivation_realm", "value": "金丹", "raw_value": "金丹"},
            fact_fields={"subject_entity_id": "ent_xiaoshi", "field": "cultivation_realm"},
            observed_ordinal=2,
            evidence_span="境界直接突破至金丹期",
        ),
        GoldenClaimSpec(
            claim_type="event",
            payload={
                "event_type": "境界突破",
                "summary": "境界直接突破至金丹期",
                "location_entity_id": None,
            },
            fact_fields={
                "event_type": "境界突破",
                "participants": ["ent_xiaoshi"],
                "location_entity_id": None,
                "sequence_in_chapter": 2,
            },
            observed_ordinal=2,
            evidence_span="境界直接突破至金丹期",
        ),
        GoldenClaimSpec(
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
                "sequence_in_chapter": 1,
            },
            observed_ordinal=3,
            evidence_span="小石真名林风",
        ),
        GoldenClaimSpec(
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
            observed_ordinal=3,
            evidence_span="是二十年前失踪的林家少主",
        ),
        GoldenClaimSpec(
            claim_type="state",
            payload={"field": "cultivation_realm", "value": "元婴", "raw_value": "元婴"},
            fact_fields={"subject_entity_id": "ent_xiaoshi", "field": "cultivation_realm"},
            observed_ordinal=3,
            operation="update",
            evidence_span="境界已达元婴",
        ),
    ]
