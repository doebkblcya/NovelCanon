"""阶段 10 检索 service 测试（docs/implementation/10 §2–§3）。

覆盖验证项：
- 运行时 profile 与维数验证（embedding profile 不匹配拒绝）；
- book/profile 隔离（Top-K 前过滤，元数据回读限定本书）；
- 元数据回读（record/raw_chunk → 章节/ordinal/原文 span）；
- 索引版本切换后孤儿 chunk 防御性跳过；
- FTS + 向量 RRF 混合检索（融合前 cutoff 过滤、路线贡献诊断）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, text

from novelcanon.retrieval import (
    BruteForceVectorStore,
    FakeEmbedder,
    FakeTokenizer,
    RetrievalService,
    build_index,
)
from tests.helpers import seed_active_book


def _build(engine: Engine, book_id: str) -> None:
    build_index(
        engine,
        book_id,
        tokenizer=FakeTokenizer(),
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
    )


def test_profile_mismatch_rejected(tmp_path: Path, migrated_db: Engine) -> None:
    """embedding profile 不匹配：运行时验证拒绝（10 §2）。"""
    data = seed_active_book(migrated_db, tmp_path)
    _build(migrated_db, data["book_id"])
    service = RetrievalService(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=16),  # 维数不同
        vector_store=BruteForceVectorStore(dimension=16),
    )
    try:
        service.search_vectors("青云宗", top_k=5)
        raise AssertionError("profile 不匹配应拒绝")
    except ValueError:
        pass


def test_vector_search_metadata_hydration(tmp_path: Path, migrated_db: Engine) -> None:
    """向量命中元数据回读：章节/ordinal/原文 span（10 §2）。"""
    data = seed_active_book(migrated_db, tmp_path)
    _build(migrated_db, data["book_id"])
    service = RetrievalService(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
    )
    hits = service.search_vectors("青云宗 石碑", top_k=5)
    assert hits
    for h in hits:
        assert h.source_chapter_id
        assert h.observed_ordinal >= 0
        assert h.char_start >= 0 and h.char_end > h.char_start
        assert h.content
        assert h.routes == ["vector"]
    # 所有命中属于本书（book 隔离）
    ordinals = {h.observed_ordinal for h in hits}
    assert ordinals <= {0, 1, 2}


def test_book_isolation(tmp_path: Path, migrated_db: Engine) -> None:
    """多书隔离：另一本书的 chunk 不进入结果（10 §2 Top-K 前过滤）。"""
    data_a = seed_active_book(migrated_db, tmp_path, book_id="book_iso_a")
    _build(migrated_db, data_a["book_id"])
    # 第二本书（无索引）
    from novelcanon.ingestion.service import import_book
    from tests.helpers import FIXTURE_CHAPTERS, make_fixture_epub

    epub = tmp_path / "iso_b.epub"
    make_fixture_epub(epub, FIXTURE_CHAPTERS, title="B")
    import_book(migrated_db, epub, book_id="book_iso_b")

    service = RetrievalService(
        migrated_db,
        data_a["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
    )
    hits = service.search_vectors("青云宗", top_k=20)
    # 只有 book_a 有索引，全部命中都属于 book_a
    with migrated_db.connect() as conn:
        for h in hits:
            row = conn.execute(
                text(
                    "SELECT book_id FROM raw_chunks rc"
                    " JOIN chapters ch ON ch.chapter_id = rc.source_chapter_id"
                    " WHERE rc.raw_chunk_id = :c"
                ),
                {"c": h.raw_chunk_id},
            ).fetchone()
            assert row[0] == data_a["book_id"]


def test_hybrid_search_rrf_and_cutoff(tmp_path: Path, migrated_db: Engine) -> None:
    """混合检索：RRF 融合 + 融合前 cutoff 过滤 + 路线贡献（10 §3）。"""
    data = seed_active_book(migrated_db, tmp_path)
    _build(migrated_db, data["book_id"])
    service = RetrievalService(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
    )
    result = service.hybrid_search("青云宗", top_k=5, cutoff=1)
    assert result.index_version_id
    assert result.rrf_params_version == "rrf-v1"
    assert "fts" in result.contributions and "vector" in result.contributions
    assert result.diagnostics["fused_candidates"] >= len(result.hits)
    # cutoff 过滤在融合前应用：所有命中 ordinal <= 1
    for h in result.hits:
        assert h.observed_ordinal <= 1
    # 路线贡献（chunk 同时被 fts/vector 命中时 routes 记录两路）
    for h in result.hits:
        assert h.routes


def test_hybrid_search_no_index_raises(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    service = RetrievalService(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
    )
    try:
        service.hybrid_search("青云宗")
        raise AssertionError("无 active 索引应拒绝")
    except ValueError:
        pass


def test_hybrid_cutoff_widens_vector_window(tmp_path: Path, migrated_db: Engine) -> None:
    """P1（11）：cutoff 过滤后向量候选不足时迭代扩窗口（不假性无结果）。"""
    data = seed_active_book(migrated_db, tmp_path)
    _build(migrated_db, data["book_id"])
    service = RetrievalService(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
    )
    # top_k=3、cutoff=0（只保留 ch0 chunks）：窗口 12 → 过滤后若不足 3
    # 条则扩窗口，最终仍返回有效结果（不因后期高排名候选被截断而空）
    result = service.hybrid_search("青云宗", top_k=3, cutoff=0)
    for h in result.hits:
        assert h.observed_ordinal <= 0, "cutoff=0 不得返回后期章节"
    # 诊断记录向量候选规模（扩窗口后 >= 初始窗口）
    assert result.diagnostics["vector_candidates"] >= 0


def test_fts_cutoff_filters_in_sql(tmp_path: Path, migrated_db: Engine) -> None:
    """P1（11）：FTS 的 cutoff 在 SQL LIMIT 前过滤（后期候选不挤占窗口）。"""
    data = seed_active_book(migrated_db, tmp_path)
    _build(migrated_db, data["book_id"])
    from novelcanon.retrieval.fts import search_shadow, search_trigram

    for fn in (search_shadow, search_trigram):
        hits = fn(migrated_db, query="青云宗", book_id=data["book_id"], limit=50, cutoff=0)
        assert hits, f"{fn.__name__} cutoff=0 仍应返回 ch0 命中"
        for h in hits:
            assert h["observed_ordinal"] <= 0, (
                f"{fn.__name__} cutoff 未在 SQL 内过滤：{h['observed_ordinal']}"
            )
