"""阶段 10 查询路由测试（docs/implementation/10 §路线表/§1）。

覆盖验证项：
- 9 类问题类型均有确定路由（结构化/混合/摘要）；
- 结构化可回答的问题不会落生成式检索兜底；
- explain 记录命中关键词与路线（可验证实际命中路线）；
- 未匹配问题显式 fallback 到混合检索并标注；
- 标准化查询（全角标点/空白归一）。
"""

from __future__ import annotations

from novelcanon.query.router import (
    PREFERRED_ROUTE,
    QueryType,
    normalize_query,
    route_question,
)


def test_entity_state_route() -> None:
    d = route_question("萧炎现在的修为是什么境界")
    assert d.query_type == QueryType.ENTITY_STATE
    assert d.route == "structured"
    assert not d.is_fallback
    assert any("修为" in kw for kw in d.matched_keywords)


def test_relation_route() -> None:
    d = route_question("药老和萧炎是什么关系")
    assert d.query_type == QueryType.RELATION
    assert d.route == "structured"


def test_org_membership_route() -> None:
    d = route_question("萧炎加入了哪个家族")
    assert d.query_type == QueryType.ORG_MEMBERSHIP
    assert d.route == "structured"


def test_relation_evolution_route() -> None:
    d = route_question("萧炎和纳兰嫣然的关系是如何变化的")
    assert d.query_type == QueryType.RELATION_EVOLUTION
    assert d.route == "structured"


def test_causal_chain_route() -> None:
    d = route_question("萧炎为什么会变成废物")
    assert d.query_type == QueryType.CAUSAL_CHAIN
    assert d.route == "structured"


def test_term_definition_route() -> None:
    d = route_question("什么是异火")
    assert d.query_type == QueryType.TERM_DEFINITION
    assert d.route == "structured"


def test_chapter_graph_route() -> None:
    d = route_question("第二章发生了什么")
    assert d.query_type == QueryType.CHAPTER_GRAPH
    assert d.route == "structured"


def test_plotline_route() -> None:
    d = route_question("这本书的主线是什么")
    assert d.query_type == QueryType.PLOTLINE
    assert d.route == "summary"


def test_raw_detail_route() -> None:
    d = route_question("青云宗不收来历不明之人这句原话在哪一章")
    assert d.query_type == QueryType.RAW_DETAIL
    assert d.route == "hybrid"
    assert not d.is_fallback  # 显式原文细节关键词，非回退


def test_structured_question_never_falls_back_to_generative() -> None:
    """结构化可回答问题不落生成式检索兜底（10 验证项）。"""
    for q in (
        "萧炎的修为",
        "纳兰嫣然与萧炎的婚约关系",
        "萧炎为什么去乌坦城",
        "萧炎所在家族",
    ):
        d = route_question(q)
        assert d.route != "hybrid", f"{q} 不应落混合检索"
        assert d.query_type in (
            QueryType.ENTITY_STATE,
            QueryType.RELATION,
            QueryType.CAUSAL_CHAIN,
            QueryType.ORG_MEMBERSHIP,
        )


def test_unknown_question_falls_back_with_explain() -> None:
    d = route_question("这本书好看吗")
    assert d.is_fallback
    assert d.query_type == QueryType.RAW_DETAIL
    assert d.route == "hybrid"
    assert "回退" in d.explain


def test_normalize_query() -> None:
    assert normalize_query("  萧炎  修为？ ") == "萧炎 修为?"
    assert normalize_query("为什么？") == "为什么?"


def test_all_types_have_preferred_route() -> None:
    for qtype in (
        QueryType.ENTITY_STATE,
        QueryType.RELATION,
        QueryType.ORG_MEMBERSHIP,
        QueryType.RELATION_EVOLUTION,
        QueryType.CAUSAL_CHAIN,
        QueryType.RAW_DETAIL,
        QueryType.TERM_DEFINITION,
        QueryType.CHAPTER_GRAPH,
        QueryType.PLOTLINE,
    ):
        assert PREFERRED_ROUTE[qtype] in ("structured", "hybrid", "summary")


def test_route_decision_hash_stable() -> None:
    d1 = route_question("萧炎修为")
    d2 = route_question("萧炎修为")
    assert d1.decision_hash == d2.decision_hash
    d3 = route_question("萧炎修为 不同问题")
    assert d1.decision_hash != d3.decision_hash
