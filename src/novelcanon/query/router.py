"""查询路由（阶段 10，docs/implementation/10 §路线表）。

按问题类型显式路由：结构化可回答的问题绝不落到生成式检索兜底
（10 验证项「结构化可回答的问题不会无故落到生成式检索」）。

路由规则为确定性关键词启发式（可解释、可评测）：每条规则记录命中的
关键词与目标路线，返回 explain 诊断；未匹配时回退到原文细节
（FTS + 向量 + RRF）或全局主线（摘要），并在 explain 中标注 fallback。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from novelcanon.config.hash import stable_config_hash

ROUTER_VERSION = "router-v2"


class QueryType:
    """问题类型（10 §路线表 9 类）。"""

    ENTITY_STATE = "entity_state"  # state claim + 双时间过滤
    RELATION = "relation"  # relation claim
    ORG_MEMBERSHIP = "org_membership"  # org 日志折叠
    RELATION_EVOLUTION = "relation_evolution"  # claim 版本时间序列
    CAUSAL_CHAIN = "causal_chain"  # event link 递归 CTE
    RAW_DETAIL = "raw_detail"  # FTS + 向量 + RRF
    TERM_DEFINITION = "term_definition"  # term definition claim
    CHAPTER_GRAPH = "chapter_graph"  # claim 双时间过滤
    PLOTLINE = "plotline"  # 分层摘要 + 关键事件


# 首选路线（10 §路线表）：类型 → 执行方式
PREFERRED_ROUTE: dict[str, str] = {
    QueryType.ENTITY_STATE: "structured",
    QueryType.RELATION: "structured",
    QueryType.ORG_MEMBERSHIP: "structured",
    QueryType.RELATION_EVOLUTION: "structured",
    QueryType.CAUSAL_CHAIN: "structured",
    QueryType.RAW_DETAIL: "hybrid",
    QueryType.TERM_DEFINITION: "structured",
    QueryType.CHAPTER_GRAPH: "structured",
    QueryType.PLOTLINE: "summary",
}

# 确定性关键词规则：问题 → 类型（有序，先命中先得）
_ROUTES: list[tuple[str, tuple[str, ...]]] = [
    (
        QueryType.PLOTLINE,
        ("主线", "概要", "总结", "概述", "总体", "梗概", "全书讲了", "剧情走向"),
    ),
    # 强原文信号优先于章节位置：问「原话在哪一章」本质是原文检索
    (
        QueryType.RAW_DETAIL,
        ("原文", "原话", "怎么说", "写道", "说了什么", "那句话"),
    ),
    (
        QueryType.CHAPTER_GRAPH,
        ("第", "章发生", "章讲了", "这章", "本章", "那一章", "章剧情", "哪一章", "第几章"),
    ),
    (
        QueryType.TERM_DEFINITION,
        ("什么是", "术语", "含义", "意思", "解释", "何为", "名词"),
    ),
    (
        QueryType.CAUSAL_CHAIN,
        ("为什么", "导致", "因为", "所以", "结果", "起因", "缘由", "因果", "引发"),
    ),
    (
        QueryType.RELATION_EVOLUTION,
        ("如何变化", "演变", "变化过程", "从什么", "变成", "什么时候开始"),
    ),
    (
        QueryType.ORG_MEMBERSHIP,
        ("势力", "宗门", "家族", "加入", "成员", "组织", "派系", "招收"),
    ),
    (
        QueryType.RELATION,
        (
            "关系",
            "师徒",
            "夫妻",
            "父子",
            "母子",
            "父女",
            "母女",
            "兄弟",
            "姐妹",
            "恋人",
            "敌人",
            "盟友",
            "同门",
            "主仆",
            "宿敌",
            # 「拜入谁的门下」是拜师关系（relation），不是组织成员
            # （阶段 11 复审 P0：QA「小石拜入谁的门下」须走 relation 路线）
            "门下",
        ),
    ),
    (
        QueryType.ENTITY_STATE,
        (
            "修为",
            "境界",
            "实力",
            "状态",
            "身份",
            "真名",
            "在哪",
            "位置",
            "拥有",
            "活着",
            "是谁",
            "目前",
            "现在",
        ),
    ),
    (
        QueryType.RAW_DETAIL,
        ("原文", "原话", "怎么说", "写道", "细节", "具体", "说了什么", "那句话", "内容"),
    ),
]


@dataclass(frozen=True)
class RouteDecision:
    """路由决策：类型、首选路线、explain 与解析出的参数。"""

    query_type: str
    route: str
    normalized_query: str
    matched_keywords: list[str] = field(default_factory=list)
    is_fallback: bool = False
    explain: str = ""

    @property
    def decision_hash(self) -> str:
        """路由决策的稳定 hash（缓存键的一部分，10 §5）。"""
        return stable_config_hash(
            {
                "query_type": self.query_type,
                "route": self.route,
                "normalized_query": self.normalized_query,
            }
        )


def normalize_query(question: str) -> str:
    """标准化查询：去除首尾空白、压缩连续空白、统一全角标点。"""
    q = " ".join(question.split())
    for full, half in (
        ("？", "?"),
        ("！", "!"),
        ("，", ","),
        ("。", "."),
        ("：", ":"),
        ("；", ";"),
    ):
        q = q.replace(full, half)
    return q.strip()


def route_question(question: str) -> RouteDecision:
    """把问题路由到确定类型与首选路线（10 §路线表）。"""
    normalized = normalize_query(question)
    matched: list[str] = []
    for qtype, keywords in _ROUTES:
        hits = [kw for kw in keywords if kw in normalized]
        if hits:
            matched.extend(hits)
            route = PREFERRED_ROUTE[qtype]
            explain = (
                f"命中类型 {qtype}（关键词 {hits}），首选路线 {route}；"
                f"该类型为结构化可回答问题，不落生成式检索兜底"
            )
            return RouteDecision(
                query_type=qtype,
                route=route,
                normalized_query=normalized,
                matched_keywords=matched,
                explain=explain,
            )
    # 未匹配：回退原文细节（FTS + 向量 + RRF 兜底），显式标注 fallback
    return RouteDecision(
        query_type=QueryType.RAW_DETAIL,
        route=PREFERRED_ROUTE[QueryType.RAW_DETAIL],
        normalized_query=normalized,
        is_fallback=True,
        explain=("未命中任何结构化类型关键词，回退到原文细节（FTS + 向量 + RRF 混合检索）"),
    )
