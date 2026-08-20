"""阶段 10 RRF 融合测试（docs/implementation/10 §3）。

覆盖验证项：
- RRF 分数 = Σ 1/(k + rank)（版本化参数）；
- 跨路线同 id 只计一次（稳定 raw_chunk_id 去重）；
- 每路线排名贡献可诊断（route_ranks）；
- FTS 影子列 + trigram 候选合并（主召回优先、trigram 补漏、去重）；
- k <= 0 拒绝。
"""

from __future__ import annotations

from novelcanon.retrieval.rrf import (
    DEFAULT_RRF_K,
    RRF_PARAMS_VERSION,
    fts_query_candidates,
    rrf_fuse,
)


def test_rrf_score_formula() -> None:
    """单路线单条：score = 1/(k+1)。"""
    fused = rrf_fuse({"fts": ["c1"]})
    assert len(fused) == 1
    assert fused[0].raw_chunk_id == "c1"
    assert fused[0].score == 1.0 / (DEFAULT_RRF_K + 1)
    assert fused[0].route_ranks == {"fts": 1}


def test_rrf_multi_route_merge() -> None:
    """两路线命中同 id：分数累加，排名各自记录。"""
    fused = rrf_fuse({"fts": ["c1", "c2"], "vector": ["c2", "c3"]})
    by_id = {f.raw_chunk_id: f for f in fused}
    assert by_id["c2"].score == 1.0 / (DEFAULT_RRF_K + 2) + 1.0 / (DEFAULT_RRF_K + 1)
    assert by_id["c2"].route_ranks == {"fts": 2, "vector": 1}
    assert by_id["c2"].routes == ["fts", "vector"]
    # 融合后按分数降序
    scores = [f.score for f in fused]
    assert scores == sorted(scores, reverse=True)


def test_rrf_custom_k() -> None:
    """版本化参数 k：k 不同分数不同，且不影响排名序。"""
    fused_a = rrf_fuse({"fts": ["c1", "c2"]}, k=10)
    fused_b = rrf_fuse({"fts": ["c1", "c2"]}, k=60)
    assert [f.raw_chunk_id for f in fused_a] == [f.raw_chunk_id for f in fused_b]
    assert fused_a[0].score != fused_b[0].score


def test_rrf_dedup_within_route() -> None:
    """同一路线内重复 id 只计一次。"""
    fused = rrf_fuse({"fts": ["c1", "c1", "c2"]})
    by_id = {f.raw_chunk_id: f for f in fused}
    assert by_id["c1"].score == 1.0 / (DEFAULT_RRF_K + 1)


def test_rrf_params_version_stable() -> None:
    assert RRF_PARAMS_VERSION == "rrf-v1"


def test_rrf_rejects_non_positive_k() -> None:
    try:
        rrf_fuse({"fts": ["c1"]}, k=0)
        raise AssertionError("k=0 应拒绝")
    except ValueError:
        pass


def test_fts_candidates_merge_order_and_dedup() -> None:
    """影子列优先 + trigram 补漏 + 同 id 去重。"""
    shadow = [{"raw_chunk_id": "a"}, {"raw_chunk_id": "b"}]
    trigram = [{"raw_chunk_id": "b"}, {"raw_chunk_id": "c"}]
    assert fts_query_candidates(shadow, trigram) == ["a", "b", "c"]
    assert fts_query_candidates([], []) == []
