"""RRF（Reciprocal Rank Fusion）融合（阶段 10 §3，docs/implementation/10）。

FTS 与向量各自产生候选和排名后，用版本化 RRF 参数融合：
- score(c) = Σ_route 1 / (k + rank_route(c))，k 为版本化参数（默认 60）；
- 融合前由调用方完成 book/cutoff 过滤（10 §3「在融合前应用
  book/cutoff 过滤」）；
- 去重基于稳定 raw_chunk_id（chunk 级唯一，跨路线同 chunk 只计一次）；
- 保留各路线贡献（命中该 chunk 的路线列表），供评测与诊断（10 §3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 版本化 RRF 参数（10 §3）：参数变化 → 版本号变化，评测与缓存可追踪。
RRF_PARAMS_VERSION = "rrf-v1"
DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class FusedRank:
    """一条融合结果：raw_chunk_id + 融合分 + 各路线排名贡献。"""

    raw_chunk_id: str
    score: float
    route_ranks: dict[str, int] = field(default_factory=dict)

    @property
    def routes(self) -> list[str]:
        return sorted(self.route_ranks)


def rrf_fuse(
    ranked_routes: dict[str, list[str]],
    *,
    k: int = DEFAULT_RRF_K,
    params_version: str = RRF_PARAMS_VERSION,
) -> list[FusedRank]:
    """把 {路线名: 候选 id 列表（按排名降序）} 融合为按分数降序的结果。

    - rank 从 1 起：列表第一项 rank=1；
    - 同一 id 在一条路线内只计一次（列表已去重）；
    - 同分时按 route_ranks 字典序稳定排序（确定性输出）。
    """
    if k <= 0:
        raise ValueError(f"RRF k 必须为正：{k}")
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for route, ids in ranked_routes.items():
        seen: set[str] = set()
        for rank, cid in enumerate(ids, start=1):
            if cid in seen:
                continue
            seen.add(cid)
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(cid, {})[route] = rank
    fused = [
        FusedRank(raw_chunk_id=cid, score=score, route_ranks=ranks[cid])
        for cid, score in scores.items()
    ]
    fused.sort(key=lambda f: (-f.score, f.raw_chunk_id))
    return fused


def fts_query_candidates(shadow_hits: list[dict], trigram_hits: list[dict]) -> list[str]:
    """FTS 影子列 + trigram 两路候选按 raw_chunk_id 去重合并。

    保留两路各自的相对序：影子列优先（主召回），trigram 补漏——
    合并顺序 = 影子列在前、trigram 补在后（同 id 只取首次出现位置）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for hits in (shadow_hits, trigram_hits):
        for h in hits:
            cid = h["raw_chunk_id"]
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out
