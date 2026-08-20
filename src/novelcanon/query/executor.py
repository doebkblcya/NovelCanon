"""查询执行器（阶段 10，docs/implementation/10 §路线表/§5/§6）。

把路由（router）→ 执行（结构化 QueryService / 混合 RetrievalService）
→ 缓存（QueryCache）→ 合成（SynthesisService）串成一条 ask() 链路：

- 结构化可回答问题走结构化路线（带证据返回），不落生成式检索；
- 原文细节走 FTS + 向量 + RRF 混合检索；
- 全局主线走分层摘要（summary_artifacts）；
- 全链路 book_id 绑定；双时间参数（cutoff / world_at）在每步生效；
- 每条路线统计（次数/延迟/来源数）供 10 §退出标准「按路线统计」。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from sqlalchemy import Engine, text

from novelcanon.query.cache import CacheKey, QueryCache, active_state_signature
from novelcanon.query.router import ROUTER_VERSION, QueryType, RouteDecision, route_question
from novelcanon.query.service import QueryService
from novelcanon.query.synthesis import (
    SYNTHESIS_SCHEMA_VERSION,
    ContextItem,
    SynthesisService,
)
from novelcanon.retrieval.service import RetrievalHit, RetrievalService
from novelcanon.retrieval.vectorstore import Embedder, VectorStore

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
# 阿拉伯/全角/汉字数字章节（如 第二章 / 第12章 / 第一百二十章）
_CHAPTER_RE = re.compile(
    r"第\s*([0-9０-９零〇一二两三四五六七八九十百]+)\s*章"
)


def _cn_to_int(token: str) -> int:
    """把「12」「二」「二十一」「一百二十」等章节 token 转为整数。"""
    if token.isdigit():
        return int(token)
    total = 0
    num = 0
    for ch in token:
        if ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        else:
            num = _CN_DIGITS.get(ch, 0)
    return total + num


@dataclass
class RouteStats:
    """按路线统计（10 退出标准：查询质量/延迟/Token 可按路线统计）。"""

    route: str
    calls: int = 0
    total_ms: float = 0.0
    context_items: int = 0
    hits: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def latency_ms(self) -> float:
        return round(self.total_ms / self.calls, 1) if self.calls else 0.0


@dataclass(frozen=True)
class AskResult:
    answer: dict
    decision: RouteDecision
    context_id: str
    knowledge_cutoff: int | None
    world_at: int | None
    cached: bool
    route_stats: dict


class QueryExecutor:
    """book_id 绑定的统一查询入口（构造时绑定，所有查询强制限定本书）。"""

    def __init__(
        self,
        engine: Engine,
        book_id: str,
        *,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        synthesis_client: object | None = None,
        profile_id: str = "",
        use_cache: bool = True,
        query_profile: str = "",
    ) -> None:
        self._engine = engine
        self._book_id = book_id
        self._query = QueryService(engine, book_id)
        self._retrieval = (
            RetrievalService(
                engine,
                book_id,
                embedder=embedder,
                vector_store=vector_store,
            )
            if embedder is not None and vector_store is not None
            else None
        )
        self._cache = QueryCache(engine, book_id)
        # 默认 query profile 版本化（10 §5：query/synthesis profile 入缓存键，
        # 查询层版本变化 → 旧缓存失效）
        if not query_profile:
            query_profile = f"v1:{ROUTER_VERSION}:{SYNTHESIS_SCHEMA_VERSION}"
        self._synthesis = SynthesisService(
            engine,
            book_id,
            client=synthesis_client,  # type: ignore[arg-type]
            profile_id=profile_id,
            query_profile=query_profile,
        )
        self._use_cache = use_cache
        self._stats: dict[str, RouteStats] = {}

    # ── 对外 ────────────────────────────────────────────────────

    def ask(
        self,
        question: str,
        *,
        knowledge_cutoff: int | None = None,
        world_at: int | None = None,
    ) -> AskResult:
        """路由 → 执行 → 缓存 → 合成（全链路双时间过滤）。"""
        decision = route_question(question)
        sig = active_state_signature(self._engine, self._book_id)
        index_version_id = self._active_index_id()
        cache_key = CacheKey(
            book_id=self._book_id,
            normalized_query=decision.normalized_query,
            query_type=decision.query_type,
            knowledge_cutoff=knowledge_cutoff,
            world_at=world_at,
            active_run_signature=sig,
            index_version_id=index_version_id,
            query_profile=self._synthesis.query_profile,
            synthesis_profile=(
                self._synthesis.profile_id if self._synthesis.has_client else "deterministic"
            ),
        )
        stats = self._stats.setdefault(
            decision.route, RouteStats(route=decision.route)
        )
        if self._use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                stats.calls += 1
                stats.cache_hits += 1
                return AskResult(
                    answer=cached,
                    decision=decision,
                    context_id=cached.get("context_id", ""),
                    knowledge_cutoff=knowledge_cutoff,
                    world_at=world_at,
                    cached=True,
                    route_stats=dict(self._stats),
                )

        started = time.perf_counter()
        context = self._execute(decision, knowledge_cutoff, world_at)
        result = self._synthesis.answer(
            question,
            route=decision.route,
            query_type=decision.query_type,
            context=context,
            knowledge_cutoff=knowledge_cutoff,
            world_at=world_at,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        stats.calls += 1
        stats.total_ms += elapsed_ms
        stats.context_items += len(context)
        stats.hits += len([c for c in context if c.kind == "chunk"])
        if result.usage is not None:
            stats.input_tokens += result.usage.input_tokens
            stats.output_tokens += result.usage.output_tokens

        payload = {
            "answer": result.answer,
            "route": result.route,
            "query_type": result.query_type,
            "sources": [
                {
                    "claim_version_id": s.claim_version_id,
                    "chapter_id": s.chapter_id,
                    "observed_ordinal": s.observed_ordinal,
                    "char_start": s.char_start,
                    "char_end": s.char_end,
                    "stance": s.stance,
                    "kind": s.kind,
                }
                for s in result.sources
            ],
            "confidence": result.confidence,
            "caveats": result.caveats,
            "context_id": result.context_id,
            "query_profile": result.query_profile,
            "knowledge_cutoff": result.knowledge_cutoff,
            "world_at": result.world_at,
            "synthesized": result.synthesized,
            "cannot_answer": result.cannot_answer,
        }
        if self._use_cache:
            self._cache.put(cache_key, payload)
        return AskResult(
            answer=payload,
            decision=decision,
            context_id=result.context_id,
            knowledge_cutoff=knowledge_cutoff,
            world_at=world_at,
            cached=False,
            route_stats=dict(self._stats),
        )

    def stats(self) -> dict[str, dict]:
        """按路线统计（10 退出标准）。"""
        return {
            route: {
                "route": s.route,
                "calls": s.calls,
                "latency_ms": s.latency_ms(),
                "context_items": s.context_items,
                "hits": s.hits,
                "cache_hits": s.cache_hits,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
            }
            for route, s in sorted(self._stats.items())
        }

    # ── 执行分发（结构化 / 混合 / 摘要）──────────────────────────

    def _execute(
        self,
        decision: RouteDecision,
        knowledge_cutoff: int | None,
        world_at: int | None,
    ) -> list[ContextItem]:
        qtype = decision.query_type
        if qtype == QueryType.RAW_DETAIL:
            return self._raw_detail(decision.normalized_query, knowledge_cutoff)
        if qtype == QueryType.PLOTLINE:
            return self._plotline(knowledge_cutoff)
        if qtype == QueryType.TERM_DEFINITION:
            return self._term(decision.normalized_query, knowledge_cutoff)
        if qtype == QueryType.CHAPTER_GRAPH:
            return self._chapter(decision.normalized_query, knowledge_cutoff, world_at)
        return self._structured(
            qtype, decision.normalized_query, knowledge_cutoff, world_at
        )

    def _structured(
        self,
        qtype: str,
        normalized: str,
        knowledge_cutoff: int | None,
        world_at: int | None,
    ) -> list[ContextItem]:
        entity = self._find_entity(normalized, knowledge_cutoff=knowledge_cutoff)
        if entity is None:
            # 结构化问题未解析出实体：返回空上下文（合成层会拒答），
            # 同时记录诊断——不静默落到生成式检索（10 验证项）。
            return []
        cid, surface = entity
        items: list[ContextItem] = []
        if qtype == QueryType.ENTITY_STATE:
            # 双时间（P0）：world_at 传入时用世界时间状态（world_state_at），
            # 否则用当前披露状态（entity_state）。
            if world_at is not None:
                states = self._query.world_state_at(
                    cid, world_at, knowledge_cutoff=knowledge_cutoff
                )
            else:
                states = self._query.entity_state(
                    cid, knowledge_cutoff=knowledge_cutoff
                )
            for s in states:
                items.append(
                    self._claim_item(s, f"{surface} 的 {s['field']} = {s['value']}")
                )
        elif qtype == QueryType.RELATION:
            for r in self._query.one_hop_relations(
                cid, knowledge_cutoff=knowledge_cutoff, world_at=world_at
            ):
                items.append(
                    self._claim_item(
                        r,
                        f"{r['from_entity_id']} —[{r['relation_type']}]→ "
                        f"{r['to_entity_id']}（原文：{r['relation_raw']}）",
                    )
                )
        elif qtype == QueryType.ORG_MEMBERSHIP:
            for m in self._query.org_membership(
                cid, knowledge_cutoff=knowledge_cutoff, world_at=world_at
            ):
                items.append(
                    self._claim_item(
                        m,
                        f"{m['member_entity_id']} ∈ {m['org_entity_id']}"
                        f"（{m['role']}，动作={m['action']}）",
                    )
                )
        elif qtype == QueryType.RELATION_EVOLUTION:
            for r in self._query.one_hop_relations(
                cid, knowledge_cutoff=knowledge_cutoff, world_at=world_at
            ):
                history = self._query.claim_history(
                    r["fact_id"], knowledge_cutoff=knowledge_cutoff
                )
                items.append(
                    self._claim_item(
                        r,
                        f"{r['from_entity_id']} —[{r['relation_type']}]→ "
                        f"{r['to_entity_id']}（版本数={len(history)}，"
                        f"最新章={r['observed_ordinal']}）",
                    )
                )
        elif qtype == QueryType.CAUSAL_CHAIN:
            for ev in self._query.entity_events(
                cid, knowledge_cutoff=knowledge_cutoff, world_at=world_at
            ):
                paths = self._query.causal_paths(
                    ev["claim_version_id"],
                    knowledge_cutoff=knowledge_cutoff,
                    world_at=world_at,
                )
                for p in paths[:5]:
                    tgt = p.get("event") or {}
                    items.append(
                        ContextItem(
                            kind="claim",
                            claim_type="causal_path",
                            claim_version_id=ev["claim_version_id"],
                            observed_ordinal=p.get("depth"),
                            content=(
                                f"因果链(置信度{p['conf']:.2f})：{ev['summary']}"
                                f" → {tgt.get('summary', p['tgt'])}"
                            ),
                            claim_status="supported",
                        )
                    )
        else:
            return []
        return items

    def _raw_detail(
        self, normalized: str, knowledge_cutoff: int | None
    ) -> list[ContextItem]:
        if self._retrieval is None:
            return []
        result = self._retrieval.hybrid_search(
            normalized, top_k=8, cutoff=knowledge_cutoff
        )
        return [self._chunk_item(h) for h in result.hits]

    def _plotline(self, knowledge_cutoff: int | None) -> list[ContextItem]:
        """全局主线：分层摘要（10 §路线表）。"""
        cutoff_sql = ""
        params: dict[str, object] = {"b": self._book_id}
        if knowledge_cutoff is not None:
            cutoff_sql = "AND max_observed_ordinal <= :cutoff"
            params["cutoff"] = knowledge_cutoff
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT summary_id, content, max_observed_ordinal, level"
                        " FROM summary_artifacts"
                        " WHERE book_id = :b AND status = 'valid'"
                        f" {cutoff_sql}"
                        " ORDER BY level, max_observed_ordinal"
                    ),
                    params,
                )
                .mappings()
                .fetchall()
            )
        items = [
            ContextItem(
                kind="summary",
                claim_type=f"summary:{r['level']}",
                claim_version_id=r["summary_id"],
                observed_ordinal=r["max_observed_ordinal"],
                content=r["content"],
            )
            for r in rows
        ]
        if not items:
            # 尚无摘要：回退全书关键事件（P1：不再只查第 0 章，
            # all_events 取 cutoff 前全部 supported 事件，限量排序）
            for ev in self._query.all_events(
                knowledge_cutoff=knowledge_cutoff, limit=30
            ):
                items.append(
                    self._claim_item(
                        ev, content=f"[{ev['event_type']}] {ev['summary']}"
                    )
                )
        return items

    def _term(
        self, normalized: str, knowledge_cutoff: int | None
    ) -> list[ContextItem]:
        term = self._find_term(normalized)
        if term is None:
            return []
        d = self._query.term_definition(term, knowledge_cutoff=knowledge_cutoff)
        if d is None:
            return []
        row = dict(d)
        row["claim_type"] = "term_definition"
        content = f"术语「{d['canonical_name']}」：{d['definition']}"
        return [self._claim_item(row, content)]

    def _chapter(
        self,
        normalized: str,
        knowledge_cutoff: int | None,
        world_at: int | None,
    ) -> list[ContextItem]:
        m = _CHAPTER_RE.search(normalized)
        if m is None:
            return []
        ordinal = _cn_to_int(m.group(1))
        items = []
        for g in self._query.chapter_graph(
            ordinal, knowledge_cutoff=knowledge_cutoff, world_at=world_at
        ):
            payload = g.get("payload") or "{}"
            items.append(
                self._claim_item(
                    g, content=f"[{g['claim_type']}] {payload}"
                )
            )
        return items

    # ── 上下文条目构造 ───────────────────────────────────────────

    @staticmethod
    def _claim_item(row: dict, content: str) -> ContextItem:
        """把查询行转为上下文：保留证据的章节与原文 span 定位（P0）。

        row 来自 QueryService（entity_state/one_hop_relations/org_membership
        /chapter_graph 等），均带 evidence（claim_evidence 行）。取第一条
        supports 证据的 chapter_id/char_start/char_end/stance，使最终
        AnswerSource 能提供「章节定位 + evidence」（10 §4 退出标准）。
        """
        evidence = row.get("evidence") or []
        primary = next(
            (e for e in evidence if e.get("evidence_stance") == "supports"),
            evidence[0] if evidence else None,
        )
        return ContextItem(
            kind="claim",
            claim_type=row.get("claim_type"),
            claim_version_id=row["claim_version_id"],
            chapter_id=primary.get("chapter_id") if primary else None,
            observed_ordinal=row.get("observed_ordinal"),
            char_start=primary.get("char_start") if primary else None,
            char_end=primary.get("char_end") if primary else None,
            content=content,
            claim_status=row.get("claim_status", "supported"),
            confidence=row.get("confidence"),
            evidence_stance=primary.get("evidence_stance", "") if primary else "",
        )

    @staticmethod
    def _chunk_item(hit: RetrievalHit) -> ContextItem:
        return ContextItem(
            kind="chunk",
            claim_type="raw_chunk",
            claim_version_id=hit.raw_chunk_id,
            chapter_id=hit.source_chapter_id,
            observed_ordinal=hit.observed_ordinal,
            char_start=hit.char_start,
            char_end=hit.char_end,
            content=hit.content,
            claim_status="supported",
            evidence_stance="supports",
        )

    # ── 实体 / 术语 / 章节解析 ──────────────────────────────────

    def _find_entity(
        self, normalized: str, *, knowledge_cutoff: int | None = None
    ) -> tuple[str, str] | None:
        """问题中最长匹配的 active 实体表面名 → (canonical_id, surface)。

        knowledge_cutoff（P0）：alias 按 observed_ordinal 截断——后期才
        披露的身份名不得在早期 cutoff 查询中参与实体解析（alias 必须
        按 observed_ordinal 截断的契约）。
        """
        cutoff_sql = ""
        params: dict[str, object] = {"b": self._book_id}
        if knowledge_cutoff is not None:
            cutoff_sql = " AND a.observed_ordinal <= :cutoff"
            params["cutoff"] = knowledge_cutoff
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT COALESCE(er.canonical_id, a.canonical_id) AS cid,"
                    " a.surface_name"
                    " FROM entity_alias_claims a"
                    " JOIN alias_observations o ON o.claim_version_id = a.claim_version_id"
                    " JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                    " LEFT JOIN entity_resolutions er ON er.mention_id = a.canonical_id"
                    " WHERE r.status = 'active' AND r.book_id = :b AND a.operation = 'assert'"
                    f"{cutoff_sql}"
                    " GROUP BY cid, a.surface_name"
                ),
                params,
            ).fetchall()
        best: tuple[str, str] | None = None
        for cid, surface in rows:
            if surface and surface in normalized and (
                best is None or len(surface) > len(best[1])
            ):
                best = (cid, surface)
        return best

    def _find_term(self, normalized: str) -> str | None:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT canonical_name FROM terms ORDER BY length(canonical_name) DESC")
            ).fetchall()
        for (name,) in rows:
            if name and name in normalized:
                return name
        return None

    def _active_index_id(self) -> str | None:
        from novelcanon.retrieval.indexer import get_active_index_version

        index = get_active_index_version(self._engine, self._book_id)
        return index["index_version_id"] if index else None

    # ── 诊断辅助 ────────────────────────────────────────────────

    def explain(self, question: str) -> dict:
        """路由 explain（10 §1：验证实际命中路线）。"""
        decision = route_question(question)
        return {
            "query_type": decision.query_type,
            "route": decision.route,
            "matched_keywords": decision.matched_keywords,
            "is_fallback": decision.is_fallback,
            "explain": decision.explain,
        }

    def route_metrics(self) -> dict:
        """按路线统计摘要（含决策 hash 可追踪）。"""
        return self.stats()
