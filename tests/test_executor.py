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

from novelcanon.query import QueryExecutor, QueryService
from novelcanon.retrieval import (
    BruteForceVectorStore,
    FakeEmbedder,
    FakeTokenizer,
    build_index,
)
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.payloads import RelationPayload
from novelcanon.schemas.types import ClaimStatus, Operation
from novelcanon.storage.repository import Repository
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


# ── 阶段 10 验收复审 P0/P1 回归 ────────────────────────────────


def test_entity_state_honors_world_at(tmp_path: Path, migrated_db: Engine) -> None:
    """P0-2：ENTITY_STATE 路线传入 world_at 时用世界时间状态。

    萧炎 alive 在 ch2（ordinal 1，chapter_proxy from=1）：world_at=0 时
    世界窗口未覆盖（不返回），world_at=1 时返回——不是当前披露状态。
    """
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    r0 = _ask(executor, "萧炎的状态如何", world_at=0)
    assert "alive" not in r0.answer["answer"], "world_at=0 不得返回 ch2 状态"
    r1 = _ask(executor, "萧炎的状态如何", world_at=1)
    assert "alive" in r1.answer["answer"], "world_at=1 应返回世界时间状态"
    # 快照同步贯通
    qs = QueryService(migrated_db, data["book_id"])
    snap0 = qs.entity_snapshot("ent_xiaoyan", world_at=0)
    assert snap0["state"] == []
    snap1 = qs.entity_snapshot("ent_xiaoyan", world_at=1)
    assert snap1["state"] and snap1["state"][0]["field"] == "alive"


def test_late_alias_blocked_by_cutoff(tmp_path: Path, migrated_db: Engine) -> None:
    """P0-3a：后期披露的 alias 在早期 cutoff 不参与实体解析。"""
    data = seed_active_book(migrated_db, tmp_path)
    repo = Repository(migrated_db)
    from novelcanon.schemas.ids import alias_fact_id
    from novelcanon.schemas.memory import AliasClaim

    # 萧炎后期化名「炎帝」（ch3 / ordinal 2 才披露）
    repo.write_alias(
        AliasClaim(
            alias_fact_id=alias_fact_id("ent_xiaoyan", "炎帝"),
            claim_version_id="",
            canonical_id="ent_xiaoyan",
            surface_name="炎帝",
            observed_ordinal=2,
            observed_chapter_id=data["chapters"][2],
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    executor = _executor(migrated_db, data)
    early = _ask(executor, "炎帝的状态如何", knowledge_cutoff=1)
    assert early.answer["cannot_answer"], "cutoff=1 时「炎帝」未披露，不得解析"
    late = _ask(executor, "炎帝的状态如何", knowledge_cutoff=2)
    assert "alive" in late.answer["answer"], "cutoff=2 时「炎帝」已披露"


def test_relation_evolution_respects_cutoff(tmp_path: Path, migrated_db: Engine) -> None:
    """P0-3b：关系演变不泄露未来版本数（claim_history 截止前版本）。"""
    data = seed_active_book(migrated_db, tmp_path)
    repo = Repository(migrated_db)
    from novelcanon.schemas.ids import relation_fact_id
    from novelcanon.schemas.payloads import RelationPayload

    fact = relation_fact_id("ent_yaolao", "师徒", "ent_xiaoyan")
    # 第 2 版：ch3（ordinal 2）关系更新为「正式收徒」
    repo.write_claim(
        ClaimEnvelope(
            fact_id=fact,
            claim_version_id="",
            claim_type="relation",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=data["chapters"][2],
            observed_ordinal=2,
            world_valid_kind="chapter_proxy",
            world_valid_from=2,
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
        RelationPayload(
            from_entity_id="ent_yaolao",
            to_entity_id="ent_xiaoyan",
            relation_type="师徒",
            relation_raw="药老正式收萧炎为徒",
        ),
    )
    qs = QueryService(migrated_db, data["book_id"])
    full = qs.claim_history(fact)
    assert len(full) == 2, "完整历史 2 个版本"
    early = qs.claim_history(fact, knowledge_cutoff=1)
    assert len(early) == 1, "cutoff=1 只含第 1 版（不泄露未来版本数）"
    # 端到端：关系演变逐版本输出（版本时间序列），cutoff 不泄露第 2 版
    executor = _executor(migrated_db, data)
    r = _ask(executor, "药老和萧炎的关系如何变化", knowledge_cutoff=1)
    assert "[版本 assert]" in r.answer["answer"]
    assert "正式收徒" not in r.answer["answer"], (
        f"cutoff=1 不得泄露第 2 版（正式收徒）：{r.answer['answer']}"
    )
    r_full = _ask(executor, "药老和萧炎的关系如何变化")
    assert "正式收萧炎为徒" in r_full.answer["answer"], "完整查询应含全部版本"


def test_structured_answer_carries_evidence_location(tmp_path: Path, migrated_db: Engine) -> None:
    """P0-4：结构化答案的 AnswerSource 带章节定位与原文 span。"""
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    r = _ask(executor, "萧炎的状态如何")
    assert r.answer["sources"], "应有证据来源"
    for s in r.answer["sources"]:
        assert s["chapter_id"], "证据必须带 chapter_id"
        assert s["char_start"] is not None, "证据必须带 char_start"
        assert s["char_end"] is not None, "证据必须带 char_end"
        assert s["observed_ordinal"] is not None


def test_llm_synthesis_usage_tracked(tmp_path: Path, migrated_db: Engine) -> None:
    """P1-5b：LLM 问答的 usage 进入 RouteStats token 统计。"""
    data = seed_active_book(migrated_db, tmp_path)
    from novelcanon.generation.client import FakeGenerationClient
    from novelcanon.pipeline.ledger import Usage

    fake = FakeGenerationClient(
        {"上下文": '{"answer":"萧炎修为三段","confidence":0.9,"caveats":[]}'},
        usage=Usage(input_tokens=120, output_tokens=60, provider="fake", model="m"),
    )
    executor = QueryExecutor(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
        synthesis_client=fake,
        profile_id="p1",
    )
    _ask(executor, "萧炎的状态如何")
    stats = executor.stats()
    structured = stats["structured"]
    assert structured["input_tokens"] == 120
    assert structured["output_tokens"] == 60


def test_plotline_fallback_uses_all_book_events(tmp_path: Path, migrated_db: Engine) -> None:
    """P1-5c：无摘要时主线回退覆盖全书关键事件（非仅第 0 章）。"""
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    r = _ask(executor, "这本书的主线是什么")
    assert r.decision.query_type == "plotline"
    sources = r.answer["sources"]
    assert sources, "无摘要时应回退关键事件"
    ordinals = {s["observed_ordinal"] for s in sources}
    assert len(ordinals) >= 2, f"回退应覆盖多章事件（当前只覆盖 {ordinals}）"


def test_causal_route_sources_are_edge_evidence(tmp_path: Path, migrated_db: Engine) -> None:
    """P0（复审）：因果路线的 AnswerSource 用因果边版本与验证证据定位。"""
    from tests.test_events import (
        _book_and_chapters,
        _link_events,
        _seed_events,
    )

    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    # test_events 数据无 alias：补陆尘 alias 供实体解析
    from novelcanon.schemas.ids import alias_fact_id
    from novelcanon.schemas.memory import AliasClaim
    from novelcanon.storage.repository import Repository

    Repository(migrated_db).write_alias(
        AliasClaim(
            alias_fact_id=alias_fact_id("ent_luchen", "陆尘"),
            claim_version_id="",
            canonical_id="ent_luchen",
            surface_name="陆尘",
            observed_ordinal=0,
            observed_chapter_id=ids[0],
            created_by_run_id=run_id,
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    _link_events(migrated_db, book_id, run_id)
    from novelcanon.query import QueryExecutor

    executor = QueryExecutor(migrated_db, book_id)
    r = executor.ask("陆尘为什么立誓报仇")
    assert r.decision.query_type == "causal_chain"
    sources = r.answer["sources"]
    causal = [s for s in sources if s["kind"] == "claim"]
    assert causal, "因果路线应有来源"
    for s in causal:
        assert s["chapter_id"], "因果来源必须带章节定位"
        assert s["char_start"] is not None and s["char_end"] is not None
        assert s["observed_ordinal"] is not None
    # 来源是因果边版本（ver_ 前缀，非起始事件）
    assert causal[0]["claim_version_id"].startswith("ver_")


def test_llm_usage_persisted_to_ledger(tmp_path: Path, migrated_db: Engine) -> None:
    """P1（复审）：LLM 问答 token 持久化到 token_ledger（stage='query'）。"""
    data = seed_active_book(migrated_db, tmp_path)
    from novelcanon.generation.client import FakeGenerationClient
    from novelcanon.pipeline.ledger import Usage

    fake = FakeGenerationClient(
        {"上下文": '{"answer":"萧炎状态三段","confidence":0.9,"caveats":[]}'},
        usage=Usage(
            input_tokens=50,
            cached_input_tokens=10,
            reasoning_tokens=5,
            output_tokens=20,
            retry_count=1,
            discarded_tokens=3,
            provider="fake",
            model="m",
        ),
    )
    executor = QueryExecutor(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
        synthesis_client=fake,
        profile_id="p1",
    )
    _ask(executor, "萧炎的状态如何")
    from sqlalchemy import text

    with migrated_db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT book_id, stage, input_tokens, cached_input_tokens,"
                " reasoning_tokens, output_tokens, retry_count, discarded_tokens"
                " FROM token_ledger WHERE stage = 'query'"
            )
        ).fetchone()
    assert row is not None, "查询 token 应写入 token_ledger"
    assert row[0] == data["book_id"]
    assert row[1] == "query"
    assert row[2] == 50 and row[3] == 10 and row[4] == 5
    assert row[5] == 20 and row[6] == 1 and row[7] == 3
    # RouteStats 全字段（P1）
    stats = executor.stats()["structured"]
    assert stats["cached_input_tokens"] == 10
    assert stats["reasoning_tokens"] == 5
    assert stats["retry_count"] == 1
    assert stats["discarded_tokens"] == 3


def test_causal_multi_hop_sources_cover_all_edges(tmp_path: Path, migrated_db: Engine) -> None:
    """P0（三轮）：多跳因果路径的 AnswerSource 覆盖全部边（含后续边）。"""
    from tests.test_events import (
        _book_and_chapters,
        _link_events,
        _seed_events,
    )

    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    from novelcanon.schemas.ids import alias_fact_id
    from novelcanon.schemas.memory import AliasClaim
    from novelcanon.storage.repository import Repository

    Repository(migrated_db).write_alias(
        AliasClaim(
            alias_fact_id=alias_fact_id("ent_luchen", "陆尘"),
            claim_version_id="",
            canonical_id="ent_luchen",
            surface_name="陆尘",
            observed_ordinal=0,
            observed_chapter_id=ids[0],
            created_by_run_id=run_id,
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    _link_events(migrated_db, book_id, run_id)
    from novelcanon.query import QueryExecutor, QueryService

    # 找 depth>=2 的路径（多跳：如 遇险→获救→立誓）
    qs = QueryService(migrated_db, book_id)
    multi_hop = False
    for ev_id in version_ids:
        for p in qs.causal_paths(ev_id):
            if len(p.get("edge_evidence") or []) >= 2:
                multi_hop = True
    assert multi_hop, "测试数据应含多跳因果路径"
    # 多跳路径的全部边 id
    expected_edges: set[str] = set()
    for ev_id in version_ids:
        for p in qs.causal_paths(ev_id):
            for e in p.get("edge_evidence") or []:
                expected_edges.add(e["claim_version_id"])
    assert len(expected_edges) >= 2, "测试数据应含至少两条已验证因果边"
    executor = QueryExecutor(migrated_db, book_id)
    r = executor.ask("陆尘为什么立誓报仇")
    source_ids = {s["claim_version_id"] for s in r.answer["sources"]}
    missing = expected_edges - source_ids
    assert not missing, f"多跳路径的后续边必须进入 AnswerSource，缺失：{missing}"
    edge_source = next(s for s in r.answer["sources"] if s["claim_version_id"] in expected_edges)
    assert edge_source["chapter_id"] and edge_source["char_start"] is not None


def test_relation_evolution_includes_ended_relations(tmp_path: Path, migrated_db: Engine) -> None:
    """P0（三轮）：最新版本 retract 的关系仍可查演变（含结束版本）。"""
    data = seed_active_book(migrated_db, tmp_path)
    repo = Repository(migrated_db)
    from novelcanon.schemas.ids import relation_fact_id

    fact = relation_fact_id("ent_xiaoyan", "恋人", "ent_nalan")
    # 第 2 版：ch3（ordinal 2）解除婚约（retract）
    repo.write_claim(
        ClaimEnvelope(
            fact_id=fact,
            claim_version_id="",
            claim_type="relation",
            operation=Operation.RETRACT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=data["chapters"][2],
            observed_ordinal=2,
            world_valid_kind="chapter_proxy",
            world_valid_from=2,
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
        RelationPayload(
            from_entity_id="ent_xiaoyan",
            to_entity_id="ent_nalan",
            relation_type="恋人",
            relation_raw="解除婚约",
        ),
    )
    # 当前关系视图已无此关系（最新版本 retract）
    qs = QueryService(migrated_db, data["book_id"])
    assert all(r["relation_type"] != "恋人" for r in qs.one_hop_relations("ent_xiaoyan"))
    # 但关系演变仍能展示建立 → 结束 完整时间线
    executor = _executor(migrated_db, data)
    r = _ask(executor, "萧炎与纳兰嫣然的关系如何变化")
    answer = r.answer["answer"]
    assert "[版本 assert]" in answer, "应有建立版本"
    assert "[版本 retract]" in answer, f"已结束的关系必须展示 retract 版本：{answer}"


def test_causal_chain_body_contains_middle_event(tmp_path: Path, migrated_db: Engine) -> None:
    """P0（四轮）：多跳因果正文含中间事件（A → B → C，非直接因果）。"""
    from tests.test_events import (
        _book_and_chapters,
        _link_events,
        _seed_events,
    )

    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    from novelcanon.schemas.ids import alias_fact_id
    from novelcanon.schemas.memory import AliasClaim
    from novelcanon.storage.repository import Repository

    Repository(migrated_db).write_alias(
        AliasClaim(
            alias_fact_id=alias_fact_id("ent_luchen", "陆尘"),
            claim_version_id="",
            canonical_id="ent_luchen",
            surface_name="陆尘",
            observed_ordinal=0,
            observed_chapter_id=ids[0],
            created_by_run_id=run_id,
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    _link_events(migrated_db, book_id, run_id)
    from novelcanon.query import QueryExecutor, QueryService

    # 找到中间事件：多跳路径（如 遇险→获救→立誓）的中间事件 summary
    qs = QueryService(migrated_db, book_id)
    middle_summary = None
    for ev_id in version_ids:
        for p in qs.causal_paths(ev_id):
            evs = [e for e in (p.get("path_events") or []) if e and e.get("summary")]
            if len(evs) >= 3:
                middle_summary = evs[1]["summary"]
                break
        if middle_summary:
            break
    assert middle_summary, "测试数据应含 3 事件多跳路径"
    executor = QueryExecutor(migrated_db, book_id)
    r = executor.ask("陆尘为什么立誓报仇")
    answer = r.answer["answer"]
    assert middle_summary in answer, (
        f"多跳正文必须含中间事件：{middle_summary!r} not in {answer[:200]}"
    )
    assert " → " in answer, "多跳正文应按 A → B → C 展示"


def test_relation_evolution_honors_world_at(tmp_path: Path, migrated_db: Engine) -> None:
    """P1（四轮）：关系演变按 world_at 过滤版本（双时间契约）。

    师徒关系 ch2 建立（chapter_proxy from=1）。
    """
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    # world_at=0：关系建立于 from=1 → 世界时间未覆盖 → 无版本
    r0 = _ask(executor, "药老和萧炎的关系如何变化", world_at=0)
    assert "师徒" not in r0.answer["answer"], (
        f"world_at=0 不得返回 from=1 的关系版本：{r0.answer['answer']}"
    )
    # world_at=1：可见
    r1 = _ask(executor, "药老和萧炎的关系如何变化", world_at=1)
    assert "师徒" in r1.answer["answer"]


def test_dual_entity_relation_narrows_endpoints(tmp_path: Path, migrated_db: Engine) -> None:
    """P1（11）：双实体问题按端点对收窄——只返回两者之间的关系。"""
    data = seed_active_book(migrated_db, tmp_path)
    executor = _executor(migrated_db, data)
    # 萧炎有：师徒（药老）、恋人（纳兰嫣然）两条关系
    r_single = _ask(executor, "萧炎与纳兰嫣然的关系")
    answer = r_single.answer["answer"]
    assert "恋人" in answer
    assert "师徒" not in answer, f"双实体问题只应返回两者之间：{answer}"
    r_pair = _ask(executor, "药老和萧炎的关系")
    assert "师徒" in r_pair.answer["answer"]
    assert "恋人" not in r_pair.answer["answer"]
