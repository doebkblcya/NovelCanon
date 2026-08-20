"""阶段 10 查询执行器端到端测试（docs/implementation/10 §路线表/§5）。

覆盖验证项：
- 结构化问题路由到结构化路线并带证据返回（不落生成式检索）；
- 原文细节走混合检索（FTS + 向量 + RRF）；
- 全局主线走分层摘要；
- 缓存命中（同问题第二次返回 cached=True）；
- 按路线统计可诊断；
- 证据不足明确拒答。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from novelcanon.query import QueryExecutor
from novelcanon.retrieval import (
    BruteForceVectorStore,
    FakeEmbedder,
    FakeTokenizer,
    build_index,
)
from tests.helpers import seed_active_book


def _executor(migrated_db: Engine, data: dict, **kw) -> QueryExecutor:
    return QueryExecutor(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
        query_profile="test",
        **kw,
    )


def _ask(executor: QueryExecutor, q: str, **kw):
    return executor.ask(q, **kw)


def test_structured_entity_state_question(tmp_path: Path, migrated_db: Engine) -> None:
    """实体状态问题：结构化路线 + 证据 + 章节定位（10 §路线表）。"""
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    result = _ask(executor, "萧炎现在的状态如何")
    assert result.decision.query_type == "entity_state"
    assert result.decision.route == "structured"
    payload = result.answer
    assert payload["route"] == "structured"
    assert "alive" in payload["answer"] or "true" in payload["answer"]
    assert payload["sources"]
    assert payload["context_id"]
    assert not result.cached


def test_structured_unknown_entity_refuses(tmp_path: Path, migrated_db: Engine) -> None:
    """结构化问题未解析出实体 → 证据不足拒答（不落生成式检索）。"""
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    result = _ask(executor, "张无忌的修为")
    assert result.decision.route == "structured"
    assert result.answer["cannot_answer"]
    assert "证据不足" in result.answer["answer"]


def test_raw_detail_hybrid_route(tmp_path: Path, migrated_db: Engine) -> None:
    """原文细节：混合检索（FTS + 向量 + RRF），chunk 来源带定位。"""
    data = seed_active_book(migrated_db, tmp_path)
    build_index(
        migrated_db,
        data["book_id"],
        tokenizer=FakeTokenizer(),
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
    )
    executor = _executor(migrated_db, data)
    result = _ask(executor, "青云宗不收来历不明之人这句原话")
    assert result.decision.query_type == "raw_detail"
    assert result.decision.route == "hybrid"
    payload = result.answer
    assert payload["route"] == "hybrid"
    sources = payload["sources"]
    assert any(s["kind"] == "chunk" for s in sources)


def test_plotline_uses_summaries(tmp_path: Path, migrated_db: Engine) -> None:
    """全局主线：分层摘要路线（无摘要时回退关键事件，仍带证据）。"""
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    result = _ask(executor, "这本书的主线是什么")
    assert result.decision.query_type == "plotline"
    assert result.decision.route == "summary"
    # 无摘要 → 回退关键事件（事件 claim 上下文）
    assert result.answer["sources"]


def test_cache_hit_on_repeat_question(tmp_path: Path, migrated_db: Engine) -> None:
    """同问题（同签名/参数）第二次命中缓存。"""
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    r1 = _ask(executor, "萧炎所在家族", knowledge_cutoff=5)
    assert not r1.cached
    r2 = _ask(executor, "萧炎所在家族", knowledge_cutoff=5)
    assert r2.cached
    # cutoff 变化 → 缓存不命中
    r3 = _ask(executor, "萧炎所在家族", knowledge_cutoff=1)
    assert not r3.cached


def test_route_stats_tracked(tmp_path: Path, migrated_db: Engine) -> None:
    """按路线统计：调用数/延迟/上下文项（10 退出标准）。"""
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    _ask(executor, "萧炎所在家族")
    _ask(executor, "萧炎所在家族")  # 缓存命中
    stats = executor.stats()
    assert "structured" in stats
    s = stats["structured"]
    assert s["calls"] == 2
    assert s["cache_hits"] == 1
    assert s["context_items"] >= 1
    assert s["latency_ms"] >= 0


def test_explain_reports_route(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    expl = executor.explain("萧炎与纳兰嫣然的关系")
    assert expl["query_type"] == "relation"
    assert expl["route"] == "structured"
    assert expl["matched_keywords"]
