"""阶段 10 查询缓存测试（docs/implementation/10 §5）。

覆盖验证项：
- 缓存键包含 book_id/标准化查询/cutoff/world/active 签名/版本/profile；
- active run 或依赖版本变化 → 签名变化 → 旧缓存不命中；
- 相同状态幂等（同签名同键）；
- 缓存结果可读回。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from novelcanon.query import QueryCache, active_state_signature
from novelcanon.query.cache import CacheKey
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.types import ClaimStatus, Operation
from tests.helpers import seed_active_book


def _key(book_id: str, engine: Engine, **kw) -> CacheKey:
    return CacheKey(
        book_id=book_id,
        normalized_query=kw.get("query", "萧炎修为"),
        query_type=kw.get("qtype", "entity_state"),
        knowledge_cutoff=kw.get("cutoff"),
        world_at=kw.get("world"),
        active_run_signature=kw.get("sig") or active_state_signature(engine, book_id),
        index_version_id=kw.get("index"),
        query_profile=kw.get("qprofile", "test"),
        synthesis_profile=kw.get("sprofile"),
    )


def test_cache_roundtrip_and_key_stability(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    cache = QueryCache(migrated_db, data["book_id"])
    key = _key(data["book_id"], migrated_db)
    assert cache.get(key) is None
    cache.put(key, {"answer": "测试答案", "context_id": "ctx1"})
    got = cache.get(key)
    assert got == {"answer": "测试答案", "context_id": "ctx1"}
    # 同状态键稳定（幂等）
    assert _key(data["book_id"], migrated_db).to_key() == key.to_key()


def test_cache_invalidated_by_cutoff_change(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    cache = QueryCache(migrated_db, data["book_id"])
    k1 = _key(data["book_id"], migrated_db, cutoff=None)
    k2 = _key(data["book_id"], migrated_db, cutoff=5)
    assert k1.to_key() != k2.to_key()
    cache.put(k1, {"answer": "a"})
    assert cache.get(k2) is None


def test_cache_invalidated_by_active_run_signature(tmp_path: Path, migrated_db: Engine) -> None:
    """active run 变化 → 签名变化 → 旧缓存不返回（10 §5 硬性要求）。"""
    data = seed_active_book(migrated_db, tmp_path)
    cache = QueryCache(migrated_db, data["book_id"])
    sig1 = active_state_signature(migrated_db, data["book_id"])
    k1 = _key(data["book_id"], migrated_db, sig=sig1)
    cache.put(k1, {"answer": "旧"})
    assert cache.get(k1)["answer"] == "旧"

    # 新增一个 active run（模拟重新抽取后激活）→ 签名变化
    from novelcanon.pipeline import RunManager
    from novelcanon.pipeline.validation import Activator
    from novelcanon.schemas.types import RunStatus

    mgr = RunManager(migrated_db)
    run2 = mgr.create(data["book_id"], input_hash="second")
    for f, t in (
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.VALIDATING),
        (RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE),
    ):
        assert mgr.transition(run2, f, t)
    assert Activator(migrated_db).activate(run2) is None

    sig2 = active_state_signature(migrated_db, data["book_id"])
    assert sig2 != sig1
    k2 = _key(data["book_id"], migrated_db, sig=sig2)
    assert cache.get(k2) is None  # 新签名下旧缓存不命中


def test_cache_key_includes_query_and_profiles(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    base = _key(data["book_id"], migrated_db)
    assert _key(data["book_id"], migrated_db, query="另一个问题").to_key() != base.to_key()
    assert _key(data["book_id"], migrated_db, qprofile="prod").to_key() != base.to_key()
    assert _key(data["book_id"], migrated_db, sprofile="synth-v2").to_key() != base.to_key()
    assert _key(data["book_id"], migrated_db, world=3).to_key() != base.to_key()


def test_summary_rebuild_invalidates_plotline_cache(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """P1-5a：同一 active run 下摘要重建 → 签名变化 → 主线缓存不命中。"""
    data = seed_active_book(migrated_db, tmp_path)
    from novelcanon.query import QueryExecutor
    from novelcanon.retrieval import BruteForceVectorStore, FakeEmbedder
    from novelcanon.summaries import DeterministicSummarizer, HierarchicalReducer

    reducer = HierarchicalReducer(
        migrated_db, data["book_id"], summarizer=DeterministicSummarizer()
    )
    reducer.reduce()

    executor = QueryExecutor(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
    )
    r1 = executor.ask("这本书的主线是什么")
    assert not r1.cached
    assert r1.answer["sources"], "应有摘要上下文"
    r2 = executor.ask("这本书的主线是什么")
    assert r2.cached, "摘要未变时应命中缓存"

    # 新增 claim → 摘要重建（新 summary_id）→ 主线缓存失效
    from novelcanon.schemas.ids import relation_fact_id
    from novelcanon.schemas.payloads import RelationPayload
    from novelcanon.storage.repository import Repository

    repo = Repository(migrated_db)
    repo.write_claim(
        ClaimEnvelope(
            fact_id=relation_fact_id("ent_xiaoyan", "师徒", "ent_yaolao"),
            claim_version_id="",
            claim_type="relation",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=data["chapters"][1],
            observed_ordinal=1,
            world_valid_kind="chapter_proxy",
            world_valid_from=1,
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
        RelationPayload(
            from_entity_id="ent_xiaoyan",
            to_entity_id="ent_yaolao",
            relation_type="师徒",
            relation_raw="萧炎拜药老为师",
        ),
    )
    reducer.reduce()
    r3 = executor.ask("这本书的主线是什么")
    assert not r3.cached, "摘要重建后旧主线缓存不得命中"
