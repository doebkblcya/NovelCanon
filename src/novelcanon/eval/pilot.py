"""Pilot 运行器（阶段 11 P1，docs/implementation/11 §P1）。

对比三路线：
1. 原文 hybrid 基线（FTS + 向量 + RRF 检索的章节定位）；
2. 原文结构化管线（QueryExecutor 结构化回答）；
3. 压缩结构化管线（真实压缩：prescan → rewrite → verify → 决策门）。

输出 PilotReport（JSON 可序列化）：各路线指标 + token 计量 + 延迟 +
成本估算 + 吞吐，以及黄金集证据 hash 复现率（正式阈值 100%）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from novelcanon.compression import ChapterCompression
from novelcanon.eval.golden import GoldenClaimSpec, GoldenSet
from novelcanon.eval.metrics import (
    causal_edge_precision,
    entity_merge_metrics,
    evidence_hash_reproduction,
    fact_metrics,
    qa_chapter_accuracy,
)
from novelcanon.eval.usage import (
    COST_PER_M_CACHED_INPUT,
    COST_PER_M_INPUT,
    COST_PER_M_OUTPUT,
    StageUsage,
    per_wan,
    pipeline_usage_ledger,
    usage_cost_usd,
)
from novelcanon.evidence.selector import (
    active_run_id as selector_active_run_id,
)
from novelcanon.evidence.selector import (
    evidence_run_condition,
)
from novelcanon.extraction.materialize import (
    GoldenClaimLike,
    GoldenEvidenceLike,
)
from novelcanon.pipeline.ledger import Usage
from novelcanon.retrieval.service import RetrievalService
from novelcanon.schemas.types import EntityTier, Operation

# 压缩重抽取抽取器类型：LLM Map 抽取器返回 (specs, usage)（正式 P1/P2），
# 简单 callable 只返回 specs（fixture/测试）——用法见 run_compressed_route。
ClaimExtractor = Callable[
    [dict[int, ChapterCompression], GoldenSet],
    list[GoldenClaimSpec] | tuple[list[GoldenClaimSpec], Usage],
]


@dataclass(frozen=True)
class PilotReport:
    """Pilot 报告：book + 各路线指标 + 汇总。"""

    book_id: str
    routes: dict[str, dict] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "book_id": self.book_id,
            "routes": self.routes,
            "summary": self.summary,
        }


def _active_claims(engine: Engine, book_id: str, *, cutoff: int | None) -> list[dict]:
    """active run 的当前事实（fact 级，供事实/证据指标）。"""
    cutoff_sql = ""
    params: dict[str, object] = {"book": book_id}
    if cutoff is not None:
        cutoff_sql = " AND c.observed_ordinal <= :cutoff"
        params["cutoff"] = cutoff
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT q.claim_version_id, q.claim_type, q.observed_ordinal,"
                    " q.operation, q.claim_status, q.payload FROM ("
                    "  SELECT c.claim_version_id, c.claim_type, c.observed_ordinal,"
                    "         c.operation, c.claim_status,"
                    "         COALESCE(s.payload, r.payload, e.payload, o.payload,"
                    "                  td.payload, '{}') AS payload,"
                    "         ROW_NUMBER() OVER (PARTITION BY c.fact_id"
                    "           ORDER BY c._rowid DESC) rn"
                    "  FROM v_active_claims c"
                    "  LEFT JOIN ("
                    "    SELECT claim_version_id,"
                    "      json_object('subject_entity_id', subject_entity_id,"
                    "                 'field', field, 'value', value) AS payload"
                    "    FROM state_claims) s"
                    "    ON s.claim_version_id = c.claim_version_id"
                    "  LEFT JOIN ("
                    "    SELECT claim_version_id,"
                    "      json_object('from_entity_id', from_entity_id,"
                    "                 'to_entity_id', to_entity_id,"
                    "                 'relation_type', relation_type) AS payload"
                    "    FROM relation_claims) r"
                    "    ON r.claim_version_id = c.claim_version_id"
                    "  LEFT JOIN ("
                    "    SELECT claim_version_id,"
                    "      json_object('event_type', event_type, 'summary', summary)"
                    "      AS payload"
                    "    FROM event_claims) e"
                    "    ON e.claim_version_id = c.claim_version_id"
                    "  LEFT JOIN ("
                    "    SELECT claim_version_id,"
                    "      json_object('org_entity_id', org_entity_id,"
                    "                 'member_entity_id', member_entity_id,"
                    "                 'role', role, 'action', action) AS payload"
                    "    FROM org_claims) o"
                    "    ON o.claim_version_id = c.claim_version_id"
                    "  LEFT JOIN ("
                    "    SELECT claim_version_id,"
                    "      json_object('term_id', term_id, 'definition', definition)"
                    "      AS payload"
                    "    FROM term_definition_claims) td"
                    "    ON td.claim_version_id = c.claim_version_id"
                    "  WHERE c.book_id = :book"
                    f"  {cutoff_sql}"
                    ") q"
                    " WHERE q.rn = 1 AND q.operation != 'retract'"
                    "   AND q.claim_status = 'supported'"
                    " ORDER BY q.observed_ordinal"
                ),
                params,
            )
            .mappings()
            .fetchall()
        )
    return [dict(r) for r in rows]


def _predict_facts(claims: list[dict]) -> list[str]:
    """从 active claims 提取事实描述（与黄金 GoldenFact.description 对齐）。"""
    return [f for per_type in _predict_facts_by_type(claims).values() for f in per_type]


def _predict_facts_by_type(claims: list[dict]) -> dict[str, list[str]]:
    """按 claim_type 分组的事实描述（逐类型 recall 断言用）。"""
    import json

    out: dict[str, list[str]] = {}
    for c in claims:
        raw = c.get("payload") or "{}"
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            payload = {}
        ctype = c["claim_type"]
        if ctype == "state":
            desc = (
                f"{payload.get('subject_entity_id', '?')} 的"
                f" {payload.get('field', '?')} = {payload.get('value', '?')}"
            )
        elif ctype == "relation":
            desc = (
                f"{payload.get('from_entity_id', '?')}"
                f" —[{payload.get('relation_type', '?')}]→"
                f" {payload.get('to_entity_id', '?')}"
            )
        elif ctype == "event":
            desc = f"[{payload.get('event_type', '?')}] {payload.get('summary', '?')}"
        elif ctype == "org":
            desc = (
                f"{payload.get('member_entity_id', '?')} ∈"
                f" {payload.get('org_entity_id', '?')}"
                f"({payload.get('role', '?')})"
            )
        elif ctype == "term_definition":
            desc = f"术语「{payload.get('term_id', '?')}」"
        else:
            continue
        out.setdefault(ctype, []).append(desc)
    return out


def _golden_facts_by_type(golden: GoldenSet) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in golden.facts:
        out.setdefault(f.claim_type, []).append(f.description)
    return out


def _entity_pairs_from_resolutions(engine: Engine, book_id: str) -> list[tuple[str, str, str]]:
    """predicted 合并对：同一 canonical 下的表面名两两组合。

    返回 (canonical_id, surface_a, surface_b)——core/all 实体 F1 分层用。
    数据源：entity_alias_claims.canonical_id（即解析目标，golden fixture
    与真实库一致）；仅统计 active run 的观察（防 staging 泄漏）。
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.canonical_id, a.surface_name"
                " FROM entity_alias_claims a"
                " JOIN alias_observations o ON o.claim_version_id = a.claim_version_id"
                " JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                " WHERE r.status = 'active' AND r.book_id = :b AND a.operation = 'assert'"
                " GROUP BY a.canonical_id, a.surface_name"
            ),
            {"b": book_id},
        ).fetchall()
    by_canonical: dict[str, list[str]] = {}
    for cid, surface in rows:
        if cid:
            by_canonical.setdefault(cid, []).append(surface)
    pairs: list[tuple[str, str, str]] = []
    for cid, surfaces in by_canonical.items():
        for i in range(len(surfaces)):
            for j in range(i + 1, len(surfaces)):
                pairs.append((cid, surfaces[i], surfaces[j]))
    return pairs


def _golden_surface_pairs(golden: GoldenSet) -> dict[str, list[tuple[str, str]]]:
    """黄金合并对按 canonical 分组（core/all 分层）。"""
    out: dict[str, list[tuple[str, str]]] = {}
    for m in golden.entity_merges:
        pairs = [
            (m.surfaces[i], m.surfaces[j])
            for i in range(len(m.surfaces))
            for j in range(i + 1, len(m.surfaces))
        ]
        out[m.canonical] = pairs
    return out


def _entity_merge_report(golden: GoldenSet, predicted: list[tuple[str, str, str]]) -> dict:
    """实体合并 F1：全部 + 核心实体（P1「核心/全部实体合并 F1」）。"""
    golden_by_canonical = _golden_surface_pairs(golden)
    golden_all = [p for pairs in golden_by_canonical.values() for p in pairs]
    predicted_all = [(a, b) for _, a, b in predicted]

    core = golden.core_canonicals
    golden_core = [p for cid, pairs in golden_by_canonical.items() if cid in core for p in pairs]
    predicted_core = [(a, b) for cid, a, b in predicted if cid in core]
    return {
        "all": entity_merge_metrics(golden_all, predicted_all),
        "core": entity_merge_metrics(golden_core, predicted_core),
    }


def _evidence_hashes(engine: Engine, book_id: str) -> set[str]:
    """全书证据 span_hash 集合（黄金证据复现指标用）。

    P1（十六轮）：按 active run exact-current-first 过滤——0017 允许多 run
    并存后，失败/历史 run 的 span 不得污染黄金证据复现；无 active run
    时返回全部（兼容测试与未激活场景）。
    """
    run_id = selector_active_run_id(engine, book_id)
    condition = evidence_run_condition() if run_id else ""
    params: dict[str, object] = {"b": book_id}
    if run_id:
        params["vr"] = run_id
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT e.span_hash FROM claim_evidence e"
                " JOIN claims c ON c.claim_version_id = e.claim_version_id"
                " JOIN chapters ch ON c.observed_chapter_id = ch.chapter_id"
                f" WHERE ch.book_id = :b{' AND ' + condition if condition else ''}"
            ),
            params,
        ).fetchall()
    return {r[0] for r in rows}


def _supported_causal_edges(engine: Engine, book_id: str) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT src.summary, tgt.summary FROM event_links l"
                " JOIN event_link_observations o ON o.claim_version_id = l.claim_version_id"
                " JOIN extraction_runs r ON r.run_id = o.extraction_run_id"
                " JOIN event_link_verifications v"
                "   ON v.claim_version_id = l.claim_version_id"
                "  AND v.extraction_run_id = r.run_id"
                " JOIN event_claims src ON src.claim_version_id = l.source_event_id"
                " JOIN event_claims tgt ON tgt.claim_version_id = l.target_event_id"
                " WHERE r.status = 'active' AND r.book_id = :b"
                "   AND v.claim_status = 'supported'"
            ),
            {"b": book_id},
        ).fetchall()
    return [(r[0] or "", r[1] or "") for r in rows]


def _usage_report(stats: dict[str, dict], *, input_chars: int = 0) -> dict:
    """按 QueryExecutor.stats() 汇总 token/延迟/成本/吞吐 + 每万字归一。

    复审 P1：输出 input_chars（查询输入字符量）与 per_wan（每万字
    token/成本/延迟），跨语料规模可比；成本按 usage.py 名义模型。
    """
    calls = sum(s.get("calls", 0) for s in stats.values())
    total_ms = sum(s.get("latency_ms", 0.0) for s in stats.values())
    input_tokens = sum(s.get("input_tokens", 0) for s in stats.values())
    cached_input = sum(s.get("cached_input_tokens", 0) for s in stats.values())
    reasoning = sum(s.get("reasoning_tokens", 0) for s in stats.values())
    output_tokens = sum(s.get("output_tokens", 0) for s in stats.values())
    discarded = sum(s.get("discarded_tokens", 0) for s in stats.values())
    retries = sum(s.get("retry_count", 0) for s in stats.values())
    tokens = input_tokens + cached_input + reasoning + output_tokens + discarded
    cost = (
        (input_tokens / 1e6) * COST_PER_M_INPUT
        + (cached_input / 1e6) * COST_PER_M_CACHED_INPUT
        + ((output_tokens + reasoning) / 1e6) * COST_PER_M_OUTPUT
    )
    report: dict = {
        "calls": calls,
        "latency_ms": round(total_ms, 1),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input,
        "reasoning_tokens": reasoning,
        "output_tokens": output_tokens,
        "discarded_tokens": discarded,
        "retry_count": retries,
        "cost_estimate_usd": round(cost, 6),
        "throughput_qps": round(calls / (total_ms / 1000), 3) if total_ms else 0.0,
    }
    if input_chars:
        report["input_chars"] = input_chars
        report["per_wan"] = {
            "tokens": per_wan(tokens, input_chars),
            "cost_usd": per_wan(cost, input_chars, digits=6),
            "latency_ms": per_wan(total_ms, input_chars, digits=2),
        }
    return report


def run_structured_qa(executor, golden: GoldenSet, *, cutoff: int | None = None) -> list[dict]:
    """结构化路线 QA：QueryExecutor 逐题回答，返回 answer payload 列表。"""
    return [executor.ask(qa.question, knowledge_cutoff=cutoff).answer for qa in golden.qas]


def run_hybrid_qa(
    service: RetrievalService, golden: GoldenSet, *, cutoff: int | None = None
) -> list[dict]:
    """原文 hybrid 基线 QA：检索章节作为来源（无结构化合成）。"""
    answers: list[dict] = []
    for qa in golden.qas:
        result = service.hybrid_search(qa.question, top_k=8, cutoff=cutoff)
        answers.append(
            {
                "sources": [
                    {
                        "observed_ordinal": h.observed_ordinal,
                        "chapter_id": h.source_chapter_id,
                    }
                    for h in result.hits
                ]
            }
        )
    return answers


def _chapter_texts(engine: Engine, book_id: str) -> dict[int, str]:
    from novelcanon.storage.repository import Repository

    repo = Repository(engine)
    chapters = repo.list_chapters(book_id)
    full = repo.get_book_text(book_id)
    return {c["ordinal"]: full[c["char_start"] : c["char_end"]] for c in chapters}


def run_compressed_route(
    engine: Engine,
    book_id: str,
    golden: GoldenSet,
    chapter_texts: dict[int, str],
    baseline: dict,
    *,
    known_surfaces: list[str] | None = None,
    claim_extractor: ClaimExtractor | None = None,
) -> dict:
    """压缩路线：真实压缩（prescan → rewrite → verify）→ 重抽取 → 查询。

    P0（阶段 11 复审）：不用「证据 span 存在」代理，而是走真实链路：
    1. 压缩章节（确定性管线，后验校验逐段回退）；
    2. **重抽取**：claim_extractor 从压缩文本产出 claims——正式 P1/P2
       传入 LLM Map 抽取器（llm-map，真实抽取 recall，返回 (specs,
       usage)）；fixture 缺省 golden-replay（黄金 claims 确定性重放，
       与基线 golden draft 落库同一 materialize 链路，oracle 辅助验证，
       不能授权启用压缩——决策门 extraction_mode 硬前置拒绝）；
       黄金 claim 恢复判定：同 claim_type 且证据 span 完全一致；
       分母恒为 golden.claims 全集（漏抽/删除计为丢失，P0）；
    3. **真实查询**：对压缩评测书 build_index + QueryExecutor 逐题回答，
       QA 章节正确率为真实检索结果（不再 all([]) 真空通过）；
    4. 决策门：后验全通过 + 生产抽取模式（llm-map）为硬前置，recall 差
       ≤0.02 / 证据复现不退化 / QA 不退化 / 成本节省 ≥10% 四条件。
    """
    from novelcanon.compression import CompressionService, decide_compression

    service = CompressionService()
    t0 = time.perf_counter()
    comps = service.compress_book(
        [(str(ordinal), text) for ordinal, text in sorted(chapter_texts.items())],
        known_surfaces=known_surfaces or [],
    )
    stage_times: dict[str, float] = {"rewrite": (time.perf_counter() - t0) * 1000}
    compressed_by_ordinal = {int(c.chapter_id): c for c in comps}

    total_orig = sum(len(t) for t in chapter_texts.values()) or 1
    total_comp = sum(len(c.compressed_text) for c in compressed_by_ordinal.values())
    retention = round(total_comp / total_orig, 4)
    fallback = sum(
        int(c.validation.get("fallback_segments") or 0) for c in compressed_by_ordinal.values()
    )
    restored_drops = sum(
        int(c.validation.get("restored_drop_segments") or 0) for c in compressed_by_ordinal.values()
    )
    validated = all(c.passed_validation for c in compressed_by_ordinal.values())

    # ── 重抽取：extractor 从压缩文本产出 claims ──────────────────
    extractor_usage = Usage()
    if claim_extractor is not None:
        # 正式 P1/P2：LLM Map 抽取器（真实抽取 recall；返回 (specs, usage)）
        t0 = time.perf_counter()
        out = claim_extractor(compressed_by_ordinal, golden)
        if isinstance(out, tuple):
            spec_list, extractor_usage = out
        else:
            spec_list = out
        stage_times["map"] = (time.perf_counter() - t0) * 1000
        extraction_mode = "llm-map"
        extractor_calls = len(compressed_by_ordinal)
    else:
        # fixture 确定性路径：黄金 claims 证据在压缩文本上重新定位
        # （与基线路线的 golden draft 落库同一 materialize 链路；
        #  oracle 辅助验证，不得授权启用压缩）
        spec_list = list(golden.claims)
        stage_times["map"] = 0.0
        extraction_mode = "golden-replay"
        extractor_calls = 0
    # 锚定：抽取器产出的 claim 证据必须能在压缩文本中重定位
    anchored_specs: list[GoldenClaimSpec] = []
    for spec in spec_list:
        comp = compressed_by_ordinal.get(spec.observed_ordinal)
        comp_text = comp.compressed_text if comp is not None else ""
        if spec.evidence_span and spec.evidence_span in comp_text:
            anchored_specs.append(spec)
    # P0：黄金 claim 恢复判定 = 同 claim_type 且证据 span 完全一致；
    # 分母 = **黄金标准全集**（golden.claims）——抽取器漏抽/压缩删除的
    # 证据必须计为丢失，不得用抽取器返回子集冒充 100%。
    recovered = [
        c
        for c in golden.claims
        if any(
            c.evidence_span and c.evidence_span == s.evidence_span and c.claim_type == s.claim_type
            for s in anchored_specs
        )
    ]
    lost = [c for c in golden.claims if c not in recovered]
    total_claims = len(golden.claims) or 1

    # ── 压缩评测书：真实落库 + 激活 + 索引 + 查询 ────────────────
    reingest: dict | None = None
    if anchored_specs:
        reingest = _reingest_compressed_book(
            engine, book_id, golden, compressed_by_ordinal, anchored_specs, timings=stage_times
        )
    qa_report = (
        reingest["qa_chapter_accuracy"]
        if reingest
        else {
            "accuracy": 0.0,
            "answered": len(golden.qas),
            "per_question": [],
        }
    )
    # P0（复审五轮）：证据复现分母必须与基线路线一致——基线用
    # golden.evidence_spans（GoldenSet 独立冻结字段），压缩路线不得改用
    # golden.claims[*].evidence_span（两者独立；人工冻结的额外证据若只在
    # evidence_spans 里，改用 claims 会缩小分母、虚高复现率、可能错误
    # 放行决策门）。统一按 golden.evidence_spans 去重。
    golden_spans_total = len(list(dict.fromkeys(golden.evidence_spans)))
    evidence_report = (
        reingest["evidence_reproduction"]
        if reingest
        else {
            "reproduction_rate": 0.0,
            "golden_count": golden_spans_total,
            "reproduced": 0,
        }
    )

    compressed_metrics = {
        "facts": {
            # 真实抽取恢复率：能在压缩文本重定位同型同证据的黄金 claim 比例
            "recall": round(len(recovered) / total_claims, 4),
            "golden_count": total_claims,
            "predicted_count": len(recovered),
            "extractor_claims": len(spec_list),
            "anchored": len(anchored_specs),
            "lost": [c.evidence_span for c in lost],
        },
        "extraction_mode": extraction_mode,
        "evidence_reproduction": evidence_report,
        "qa_chapter_accuracy": qa_report,
        "validated": validated,
    }
    decision = decide_compression(
        baseline, compressed_metrics, cost_saving=round(1.0 - retention, 4)
    )
    # 每万字全链路用量账本（复审 P1）：rewrite/map/disambiguation/
    # evidence_verify/link/reduce 各阶段的 token/时间/成本 + 归一化。
    # totals/per_wan 分母 = **原始语料字数**（corpus_chars=total_orig），
    # 不得把各阶段 input_chars 相加（同一语料过 5 个阶段会算成 5 万字，
    # 低估每万字指标）；阶段行保留各自 input_chars。
    ran_reingest = reingest is not None
    usage_ledger = pipeline_usage_ledger(
        [
            StageUsage(
                "rewrite",
                input_chars=total_orig,
                calls=len(compressed_by_ordinal),
                elapsed_ms=stage_times.get("rewrite", 0.0),
            ),
            StageUsage(
                "map",
                input_chars=total_comp,
                calls=extractor_calls,
                tokens=extractor_usage.total(),
                elapsed_ms=stage_times.get("map", 0.0),
                cost_usd=usage_cost_usd(extractor_usage),
                executed=claim_extractor is not None,
            ),
            StageUsage(
                "disambiguation",
                input_chars=total_comp,
                elapsed_ms=stage_times.get("disambiguation", 0.0),
                executed=ran_reingest,
            ),
            StageUsage(
                "evidence_verify",
                input_chars=total_comp,
                elapsed_ms=stage_times.get("evidence_verify", 0.0),
                executed=ran_reingest,
            ),
            StageUsage(
                "link",
                input_chars=total_comp,
                elapsed_ms=stage_times.get("link", 0.0),
                executed=ran_reingest,
            ),
            StageUsage("reduce", input_chars=0, executed=False),
        ],
        corpus_chars=total_orig,
    )
    return {
        **compressed_metrics,
        "retention": retention,
        "fallback_segments": fallback,
        "restored_drop_segments": restored_drops,
        "chapters": len(compressed_by_ordinal),
        "reingest_book_id": reingest["book_id"] if reingest else None,
        "usage": usage_ledger,
        "decision": decision.as_dict(),
    }


def _reingest_compressed_book(
    engine: Engine,
    book_id: str,
    golden: GoldenSet,
    compressed_by_ordinal: dict[int, ChapterCompression],
    anchored: list[GoldenClaimSpec],
    *,
    timings: dict[str, float] | None = None,
) -> dict:
    """把压缩文本 + 重定位的 claims 真实落库为压缩评测书。

    **独立临时库**（P0：fact_id/version_id 全局唯一，与原书同库会被幂等
    合并导致 claims 指向原书章节；独立库保证重抽取走完整 materialize
    契约——证据 span hash 校验、claim 版本、alias、observation、激活、
    索引）。走真实查询后返回指标。

    timings：按阶段记录耗时（disambiguation / evidence_verify / link），
    供每万字全链路用量账本（复审 P1）。
    """
    import tempfile
    from pathlib import Path

    from novelcanon.config.hash import stable_config_hash
    from novelcanon.extraction import materialize_draft
    from novelcanon.ingestion.normalize import sha256
    from novelcanon.pipeline import RunSummary, finish_run
    from novelcanon.pipeline.checkpoint import CheckpointService
    from novelcanon.pipeline.run import RunManager
    from novelcanon.retrieval import (
        BruteForceVectorStore,
        FakeEmbedder,
        FakeTokenizer,
        build_index,
    )
    from novelcanon.schemas.types import RunStatus
    from novelcanon.storage.engine import create_db_engine
    from novelcanon.storage.migrations import migrate_to_head
    from novelcanon.storage.repository import Repository

    timings = timings if timings is not None else {}
    suffix = stable_config_hash({"book": book_id, "ordinals": sorted(compressed_by_ordinal)})[:10]
    cbook = f"{book_id[:38]}:comp:{suffix}"

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "compressed.db"
        migrate_to_head(db_path)
        ceng = create_db_engine(db_path)
        try:
            repo = Repository(ceng)
            # 全书 normalized_text + 章节区间（证据 hash 校验基于
            # books.normalized_text）
            texts = [
                (ordinal, c.compressed_text) for ordinal, c in sorted(compressed_by_ordinal.items())
            ]
            full = "".join(t for _, t in texts)
            repo.create_book(
                cbook,
                title=f"{book_id} 压缩评测书（compression pilot）",
                source_format="compression-pilot",
                normalized_text=full,
                normalized_content_hash=sha256(full),
            )
            offset = 0
            chapter_ids: dict[int, str] = {}
            for ordinal, text in texts:
                cid = f"{cbook}:ch{ordinal}"
                chapter_ids[ordinal] = cid
                repo.create_chapter(
                    cid,
                    cbook,
                    ordinal,
                    title=f"压缩章 {ordinal}",
                    char_start=offset,
                    char_end=offset + len(text),
                    content_hash=sha256(text),
                )
                offset += len(text)

            # ── mentions（alias 供实体解析 + entities 行满足 FK）──
            # 从 entity_merges 的表面名 + claims 中出现的全部 canonical
            # 生成 mention（未出现在 merges 的 canonical 以 id 占位）。
            # 注意：实体 ID 集合必须包含**真实重抽取输出**（anchored）中
            # 的实体——LLM 可能抽取黄金集之外的实体（如「青云宗的弟子」），
            # 缺失其 mention 行会导致 materialize 时 FK 失败（真实冒烟抓到）。
            mentions: list[tuple[str, str]] = []
            canonical_map: dict[str, str] = {}

            def _claim_entity_ids() -> set[str]:
                out: set[str] = set()
                for spec in [*golden.claims, *anchored]:
                    for src in (spec.payload, spec.fact_fields):
                        for k, v in src.items():
                            if k.endswith("_entity_id") and isinstance(v, str) and v:
                                out.add(v)
                            if k == "participants" and isinstance(v, list):
                                out.update(str(x) for x in v if x)
                return out

            claim_entity_ids = _claim_entity_ids()
            covered: set[str] = set()
            for m in golden.entity_merges:
                for i, surface in enumerate(m.surfaces):
                    mid = f"m_{m.canonical}_{i}"
                    canonical_map[mid] = m.canonical
                    mentions.append((mid, surface))
                    covered.add(m.canonical)
            for cid_ in sorted(claim_entity_ids - covered):
                mid = f"m_{cid_}"
                canonical_map[mid] = cid_
                mentions.append((mid, cid_))

            run_id = RunManager(ceng).create(cbook, input_hash=suffix)
            assert RunManager(ceng).transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)

            # ── disambiguation：mentions/alias + canonical 消歧准备 ──
            t0 = time.perf_counter()
            # 按章 materialize（claims 按 observed_ordinal 分发）
            claims_by_ordinal: dict[int, list[GoldenClaimSpec]] = {}
            for spec in anchored:
                claims_by_ordinal.setdefault(spec.observed_ordinal, []).append(spec)
            timings["disambiguation"] = (time.perf_counter() - t0) * 1000

            # ── evidence_verify：逐章 materialize（证据 hash + claim 落库）──
            t0 = time.perf_counter()
            for ordinal, cid in chapter_ids.items():
                comp_text = compressed_by_ordinal[ordinal].compressed_text
                claims = [
                    _make_reingest_claim(s, cid, comp_text)
                    for s in claims_by_ordinal.get(ordinal, [])
                ]
                draft = _ReingestDraft(
                    chapter_id=cid,
                    ordinal=ordinal,
                    mentions=mentions,
                    claims=claims,
                    entity_tiers={},
                )
                materialize_draft(
                    ceng,
                    run_id=run_id,
                    book_id=cbook,
                    draft=draft,
                    canonical_map=canonical_map,
                    chapter_text=comp_text,
                    repo=repo,
                )
                # checkpoint（Validator 按 run_checkpoints 判定完成章数）
                CheckpointService(ceng).save(
                    run_id,
                    {
                        "book_id": cbook,
                        "chapter_id": cid,
                        "content_hash": sha256(comp_text),
                        "pipeline_version": "compression-pilot",
                        "prompt_version": "compression-pilot-v1",
                        "compression_version": "compression-v1",
                        "schema_version": "v1",
                    },
                    {"chapter_id": cid},
                )
            timings["evidence_verify"] = (time.perf_counter() - t0) * 1000

            # ── link：事件链接/验证 + 激活 ──────────────────────────
            t0 = time.perf_counter()
            issues = finish_run(
                ceng,
                run_id,
                total_chapters=len(chapter_ids),
                summary=RunSummary(
                    total=len(chapter_ids),
                    completed=len(chapter_ids),
                    reused=0,
                    failed=0,
                    failed_chapters=[],
                    writer_failures=0,
                ),
            )
            timings["link"] = (time.perf_counter() - t0) * 1000
            if issues:
                raise RuntimeError(f"压缩评测书激活失败：{issues}")

            build_index(
                ceng,
                cbook,
                tokenizer=FakeTokenizer(),
                embedder=FakeEmbedder(8),
                vector_store=BruteForceVectorStore(8),
            )

            from novelcanon.query import QueryExecutor

            executor = QueryExecutor(
                ceng,
                cbook,
                embedder=FakeEmbedder(dimension=8),
                vector_store=BruteForceVectorStore(dimension=8),
            )
            answers = run_structured_qa(executor, golden)
            qa_report = qa_chapter_accuracy(answers, golden.qas)

            # P0（复审五轮）：证据复现率分母 = **完整冻结黄金证据集**
            # （golden.evidence_spans，与基线路线同一 canonical 集合——
            # 不得改用 claims 的 span 缩小分母）。压缩删除/漏抽的证据必须
            # 反映为复现率下降，不得用 anchored 子集冒充 100%。
            # 复用 evidence_hash_reproduction：空集合不除零（复审 P1）。
            evidence_report = evidence_hash_reproduction(
                list(dict.fromkeys(golden.evidence_spans)), _evidence_hashes(ceng, cbook)
            )
        finally:
            ceng.dispose()

    return {
        "book_id": cbook,
        "qa_chapter_accuracy": qa_report,
        "evidence_reproduction": evidence_report,
        "materialized_claims": len(anchored),
    }


@dataclass(frozen=True)
class GoldenEvidence:
    """证据最小契约（materialize GoldenEvidenceLike）。"""

    chapter_id: str
    char_start: int
    char_end: int
    span_text: str


@dataclass(frozen=True)
class _ReingestDraft:
    """GoldenDraftLike 实现：压缩章节 + 重定位 claims（真实重抽取）。"""

    chapter_id: str
    ordinal: int
    mentions: list[tuple[str, str]]
    claims: list[GoldenClaimLike]
    entity_tiers: Mapping[str, EntityTier]


@dataclass
class _ReingestClaim:
    """GoldenClaimLike 实现：证据 span 在压缩文本重定位。

    非 frozen（Protocol 要求可写属性）。
    """

    claim_type: str
    operation: Operation
    fact_fields: Mapping[str, object]
    payload: dict
    observed_chapter_id: str
    observed_ordinal: int
    evidence: GoldenEvidenceLike | list


def _make_reingest_claim(spec: GoldenClaimSpec, chapter_id: str, comp_text: str) -> GoldenClaimLike:
    """把 GoldenClaimSpec 适配为 materialize 的 GoldenClaimLike。

    证据 span 在压缩文本上重新定位（真实重抽取锚定）；event 的
    fact_fields 按 observed chapter 补全 chapter_id。
    """
    fact_fields = dict(spec.fact_fields)
    if spec.claim_type == "event" and "chapter_id" not in fact_fields:
        fact_fields["chapter_id"] = chapter_id
    idx = comp_text.find(spec.evidence_span)
    assert idx >= 0, f"证据 span 未定位：{spec.evidence_span}"
    return _ReingestClaim(
        claim_type=spec.claim_type,
        operation=Operation(spec.operation),
        fact_fields=fact_fields,
        payload=dict(spec.payload),
        observed_chapter_id=chapter_id,
        observed_ordinal=spec.observed_ordinal,
        evidence=GoldenEvidence(chapter_id, idx, idx + len(spec.evidence_span), spec.evidence_span),
    )


def validate_golden_against_book(
    engine: Engine,
    book_id: str,
    golden: GoldenSet,
    *,
    require_content_hash: bool = False,
) -> list[str]:
    """校验黄金集与目标书匹配（P0/P1：正式评测不得错配）。

    检查 book_id、章节 ordinal 范围、书内容 hash、证据 span 存在性。
    require_content_hash=True（正式 --golden 模式）时，缺失书内容 hash
    直接拒绝——防止同 book_id 的旧标注用于已修改文本。
    返回错误列表（空列表 = 通过）。
    """
    from novelcanon.ingestion.normalize import sha256
    from novelcanon.storage.repository import Repository

    errors: list[str] = []
    if golden.book_id != book_id:
        errors.append(f"黄金集 book_id={golden.book_id} != 目标书 {book_id}")
    if require_content_hash and not golden.book_content_hash:
        errors.append(
            "黄金集缺少 book_content_hash：正式评测必须携带书内容 hash，防止旧标注错配到已修改文本"
        )
    repo = Repository(engine)
    chapters = repo.list_chapters(book_id)
    if not chapters:
        errors.append(f"目标书 {book_id} 没有章节")
        return errors
    max_ordinal = max(c["ordinal"] for c in chapters)
    for qa in golden.qas:
        for o in qa.chapter_ordinals:
            if not 0 <= o <= max_ordinal:
                errors.append(f"QA「{qa.question}」期望章节 {o} 超出范围 [0,{max_ordinal}]")
    for f in golden.facts:
        if not 0 <= f.chapter_ordinal <= max_ordinal:
            errors.append(f"事实「{f.description}」章节 {f.chapter_ordinal} 超出范围")
    for c in golden.claims:
        if not 0 <= c.observed_ordinal <= max_ordinal:
            errors.append(
                f"claim「{c.claim_type} {c.evidence_span[:16]}…」章节 {c.observed_ordinal} 超出范围"
            )

    full = repo.get_book_text(book_id)
    if golden.book_content_hash and sha256(full) != golden.book_content_hash:
        errors.append(
            f"书内容 hash 不匹配：黄金集 {golden.book_content_hash[:12]}…"
            f" != 实际 {sha256(full)[:12]}…"
        )
    # 证据 span 必须逐字存在于对应章节文本（claims/facts 的标注锚点）
    chapter_texts = {c["ordinal"]: full[c["char_start"] : c["char_end"]] for c in chapters}
    for f in golden.facts:
        if f.evidence_span and f.evidence_span not in chapter_texts.get(f.chapter_ordinal, ""):
            errors.append(f"事实「{f.description}」证据 span 不在第 {f.chapter_ordinal} 章文本中")
    for c in golden.claims:
        if c.evidence_span and c.evidence_span not in chapter_texts.get(c.observed_ordinal, ""):
            errors.append(
                f"claim「{c.claim_type} {c.evidence_span[:16]}…」证据 span"
                f" 不在第 {c.observed_ordinal} 章文本中"
            )
    # canonical evidence_spans（复审 P1）：压缩路线与基线路线的共同复现
    # 分母——正式评测必须非空且逐项存在于原书文本（防拼写错误/不存在的
    # span 直接进入分母；空集导致复现率除零）。
    if require_content_hash and not golden.evidence_spans:
        errors.append(
            "正式评测要求非空 canonical evidence_spans（golden.evidence_spans"
            " 为空时复现率无法定义）"
        )
    for i, span in enumerate(golden.evidence_spans):
        if not span:
            errors.append(f"canonical evidence_spans[{i}] 为空字符串")
        elif span not in full:
            errors.append(f"canonical evidence_spans[{i}]「{span[:16]}…」不存在于原书文本")
    return errors


def run_pilot(
    engine: Engine,
    book_id: str,
    golden: GoldenSet,
    *,
    cutoff: int | None = None,
    structured: bool = True,
    hybrid: bool = True,
    compressed: bool = False,
    known_surfaces: list[str] | None = None,
    claim_extractor: ClaimExtractor | None = None,
) -> PilotReport:
    """运行三路线 Pilot（压缩路线为真实压缩 + 决策门）。

    claim_extractor：压缩路线的重抽取器（缺省 golden-replay——oracle
    辅助验证，只适合 fixture，不能授权启用压缩；正式 P1/P2 传 LLM Map
    抽取器测真实抽取 recall——由决策门 extraction_mode 硬前置把关）。
    """
    from novelcanon.query import QueryExecutor
    from novelcanon.retrieval.factory import NoActiveIndexError, backend_for_active_index

    routes: dict[str, dict] = {}
    claims = _active_claims(engine, book_id, cutoff=cutoff)
    predicted_facts = _predict_facts(claims)
    predicted_by_type = _predict_facts_by_type(claims)
    golden_fact_descs = [f.description for f in golden.facts]
    golden_by_type = _golden_facts_by_type(golden)
    golden_causal_pairs = [(c.source, c.target) for c in golden.causals]
    question_chars = sum(len(qa.question) for qa in golden.qas)

    common: dict = {
        "facts": fact_metrics(golden_fact_descs, predicted_facts),
        "facts_by_type": {
            t: fact_metrics(golden_by_type.get(t, []), predicted_by_type.get(t, []))
            for t in sorted(set(golden_by_type) | set(predicted_by_type))
        },
        "entity_merges": _entity_merge_report(
            golden, _entity_pairs_from_resolutions(engine, book_id)
        ),
        "evidence_reproduction": evidence_hash_reproduction(
            list(golden.evidence_spans), _evidence_hashes(engine, book_id)
        ),
        "causal_edges": causal_edge_precision(
            golden_causal_pairs,
            _supported_causal_edges(engine, book_id),
        ),
    }

    # 原文路线的运行时后端按 active index 统一创建（复审 D P1）：真实索引
    # （text-embedding-v4）必须用真实 adapter——structured 的 raw-detail 与
    # hybrid 必然访问向量索引，固定 fake-embed-v8 会 profile mismatch。
    # 压缩路线的临时库主动建 fake 索引，仍用 FakeEmbedder（见
    # run_compressed_route/_make_reingest）。
    # 仅 NoActiveIndexError 做 fake 兜底；配置校验 ValueError 原样上报（P2）。
    route_backend = None
    if structured or hybrid:
        try:
            route_backend = backend_for_active_index(engine, book_id)
        except NoActiveIndexError:
            # 无 active 索引：structured 纯结构化查询可跑（raw-detail 无向量）
            from novelcanon.retrieval.vectorstore import BruteForceVectorStore, FakeEmbedder

            route_backend = (FakeEmbedder(dimension=8), BruteForceVectorStore(dimension=8))
    route_embedder, route_store = route_backend or (None, None)

    try:
        if structured:
            assert route_embedder is not None and route_store is not None
            executor = QueryExecutor(
                engine,
                book_id,
                embedder=route_embedder,
                vector_store=route_store,
            )
            started = time.perf_counter()
            answers = run_structured_qa(executor, golden, cutoff=cutoff)
            elapsed = (time.perf_counter() - started) * 1000
            routes["structured"] = {
                **common,
                "qa_chapter_accuracy": qa_chapter_accuracy(answers, golden.qas),
                "latency_ms": round(elapsed, 1),
                "usage": _usage_report(executor.stats(), input_chars=question_chars),
                "answers": answers,
            }
        if hybrid:
            assert route_embedder is not None and route_store is not None
            service = RetrievalService(
                engine,
                book_id,
                embedder=route_embedder,
                vector_store=route_store,
            )
            started = time.perf_counter()
            answers = run_hybrid_qa(service, golden, cutoff=cutoff)
            elapsed = (time.perf_counter() - started) * 1000
            routes["hybrid"] = {
                **common,
                "qa_chapter_accuracy": qa_chapter_accuracy(answers, golden.qas),
                "latency_ms": round(elapsed, 1),
                "usage": _usage_report({}, input_chars=question_chars),
                "answers": answers,
            }
        if compressed:
            chapter_texts = _chapter_texts(engine, book_id)
            baseline = routes.get("structured") or common
            routes["compressed"] = run_compressed_route(
                engine,
                book_id,
                golden,
                chapter_texts,
                baseline,
                known_surfaces=known_surfaces,
                claim_extractor=claim_extractor,
            )

        structured_route = routes.get("structured", {})
        hybrid_route = routes.get("hybrid", {})
        compressed_route = routes.get("compressed", {})
        summary = {
            "structured_qa_accuracy": structured_route.get("qa_chapter_accuracy", {}).get(
                "accuracy", 0.0
            ),
            "hybrid_qa_accuracy": hybrid_route.get("qa_chapter_accuracy", {}).get("accuracy", 0.0),
            "fact_f1": structured_route.get("facts", {}).get("f1", 0.0),
            "fact_recall": structured_route.get("facts", {}).get("recall", 0.0),
            "entity_merge_f1_all": structured_route.get("entity_merges", {})
            .get("all", {})
            .get("f1", 0.0),
            "entity_merge_f1_core": structured_route.get("entity_merges", {})
            .get("core", {})
            .get("f1", 0.0),
            "evidence_reproduction_rate": structured_route.get("evidence_reproduction", {}).get(
                "reproduction_rate", 0.0
            ),
            "causal_precision": structured_route.get("causal_edges", {}).get("precision", 0.0),
            "compression_enable": compressed_route.get("decision", {}).get("enable", False),
            "compression_retention": compressed_route.get("retention", 0.0),
            "structured_usage": structured_route.get("usage", {}),
        }
        return PilotReport(book_id=book_id, routes=routes, summary=summary)
    finally:
        # 真实 adapter 的 httpx 连接池用完释放（fake 无 close，防御 hasattr）
        # finally：评测中途异常（QA/压缩路线抛错）也不泄漏（复审 D P2）
        if route_embedder is not None:
            closer = getattr(route_embedder, "close", None)
            if closer is not None:
                closer()
