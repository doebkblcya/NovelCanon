"""查询（阶段 05/10）：结构化查询、路由、缓存与证据接地合成。"""

from novelcanon.query.cache import CacheKey, QueryCache, active_state_signature
from novelcanon.query.executor import AskResult, QueryExecutor, RouteStats
from novelcanon.query.router import (
    PREFERRED_ROUTE,
    QueryType,
    RouteDecision,
    normalize_query,
    route_question,
)
from novelcanon.query.service import QueryService
from novelcanon.query.synthesis import (
    AnswerResult,
    AnswerSource,
    ContextItem,
    SynthesisService,
)

__all__ = [
    "AnswerResult",
    "AnswerSource",
    "AskResult",
    "CacheKey",
    "ContextItem",
    "PREFERRED_ROUTE",
    "QueryCache",
    "QueryExecutor",
    "QueryService",
    "QueryType",
    "RouteDecision",
    "RouteStats",
    "SynthesisService",
    "active_state_signature",
    "normalize_query",
    "route_question",
]
