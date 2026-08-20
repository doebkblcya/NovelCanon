"""压缩评测的真实 LLM Map 抽取器（阶段 11 复审 P0）。

正式压缩 Pilot 的 CLI 路径必须注入生产 Map 抽取器（llm-map），不得
静默回退 golden-replay（黄金答案直接落库，无法衡量真实抽取 recall）。

MapClaimExtractor 复用阶段 06 的 Map 流水线
（``build_map_process_fn``：GenerationClient + 版本化 prompt + 窗口
分段 + Draft 校验），对压缩文本逐章**真实抽取**，产出
``GoldenClaimSpec``（证据 span 在压缩文本上重定位）+ 累计 Usage（供
每万字账本）。

- 黄金实体合并对仅用于 surface→canonical 消歧（disambiguation 阶段的
  职责：同一实体的不同表面名映射到黄金 canonical id），**不注入黄金
  claim 内容**——claim 的 claim_type/payload/证据 span 全部来自 LLM
  Map 输出；
- 证据无法在压缩文本锚定的 claim 被丢弃 → 计为不可复现（recall 下降，
  诚实反映真实抽取质量）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from novelcanon.compression import ChapterCompression
from novelcanon.config.settings import AppSettings, GenerationProfile
from novelcanon.eval.golden import GoldenClaimSpec, GoldenSet
from novelcanon.pipeline.ledger import Usage
from novelcanon.pipeline.runner import ChapterTask, ProcessResult

# 阶段 06 Map 流水线的 process_fn 类型（build_map_process_fn 返回）。
MapProcessFn = Callable[[ChapterTask], Awaitable[ProcessResult]]


class MapClaimExtractor:
    """真实 LLM Map 抽取器：压缩文本 → GoldenClaimSpec 列表 + Usage。

    构造参数为 Map process_fn（通常来自 ``build_map_extractor`` 或测试
    注入的 fake process_fn）。
    """

    def __init__(
        self,
        process_fn: MapProcessFn,
        *,
        profile_id: str = "eval-llm-map",
    ) -> None:
        self._process_fn = process_fn
        self.profile_id = profile_id
        self.calls = 0

    def __call__(
        self,
        compressed: Mapping[int, ChapterCompression],
        golden: GoldenSet,
    ) -> tuple[list[GoldenClaimSpec], Usage]:
        """对全部压缩章节逐章真实 Map 抽取。

        returns (specs, usage)：specs 为证据在压缩文本锚定的 claims
        （未锚定者丢弃——计为不可复现）；usage 为各章各段调用累计计量。
        """
        tasks = [
            ChapterTask(
                chapter_id=comp.chapter_id,
                ordinal=ordinal,
                content=comp.compressed_text,
                checkpoint_fields={},
            )
            for ordinal, comp in sorted(compressed.items())
        ]
        self.calls = len(tasks)
        results = asyncio.run(self._run(tasks))
        surface_map = {s: m.canonical for m in golden.entity_merges for s in m.surfaces}

        def resolve(surface: str) -> str:
            return surface_map.get(surface, surface)

        specs: list[GoldenClaimSpec] = []
        total = Usage()
        for task, res in zip(tasks, results, strict=True):
            total = total + res.usage
            draft = (res.payload or {}).get("draft")
            if not draft:
                continue
            for claim in draft.get("provisional_claims", []):
                spec = _provisional_to_spec(claim, task.ordinal, task.content, resolve)
                if spec is not None:
                    specs.append(spec)
        return specs, total

    async def _run(self, tasks: list[ChapterTask]) -> list[ProcessResult]:
        return list(await asyncio.gather(*(self._process_fn(t) for t in tasks)))


def _evidence_span(ctype: str, payload: dict, chapter_text: str) -> str:
    """从 claim payload 的原文字段挑选可在压缩文本锚定的证据 span。

    候选按类型从 LLM 输出中提取（relation_raw / raw_value / summary /
    definition / 成员表面名），**不读黄金集**——只依赖 Map 输出本身。
    优先返回在文本中出现的最长候选；无候选可锚定 → ""（丢弃）。
    """
    candidates: list[str] = []
    if ctype == "relation":
        candidates = [str(payload.get("relation_raw") or "")]
    elif ctype == "state":
        candidates = [str(payload.get("raw_value") or ""), str(payload.get("value") or "")]
    elif ctype == "event":
        candidates = [str(payload.get("summary") or "")]
    elif ctype == "org":
        candidates = [
            str(payload.get("member_entity_id") or ""),
            str(payload.get("org_entity_id") or ""),
            str(payload.get("role") or ""),
        ]
    elif ctype == "term_definition":
        candidates = [str(payload.get("definition") or "")]
    anchored = [c for c in candidates if c and c in chapter_text]
    if not anchored:
        return ""
    return max(anchored, key=len)


def _provisional_to_spec(
    claim: dict,
    ordinal: int,
    chapter_text: str,
    resolve: Callable[[str], str],
) -> GoldenClaimSpec | None:
    """把 draft 的 provisional_claim 转为 GoldenClaimSpec（含消歧）。

    关键字段缺失或证据无法锚定 → None（该 claim 计为不可复现）。
    """
    ctype = str(claim.get("claim_type") or "")
    operation = str(claim.get("operation") or "assert")
    payload = claim.get("payload") or {}
    span = _evidence_span(ctype, payload, chapter_text)
    if not span:
        return None
    out_payload, fact_fields = _typed_fields(ctype, payload, span, resolve)
    if out_payload is None or fact_fields is None:
        return None
    return GoldenClaimSpec(
        claim_type=ctype,
        payload=out_payload,
        fact_fields=fact_fields,
        observed_ordinal=ordinal,
        operation=operation,
        evidence_span=span,
    )


def _typed_fields(
    ctype: str,
    payload: dict,
    span: str,
    resolve: Callable[[str], str],
) -> tuple[dict | None, dict | None]:
    """类型专属 payload/fact_fields（surface → canonical 消歧后）。"""
    if ctype == "relation":
        frm = resolve(str(payload.get("from_entity_id") or ""))
        to = resolve(str(payload.get("to_entity_id") or ""))
        rtype = str(payload.get("relation_type") or "")
        if not frm or not to or not rtype:
            return None, None
        return (
            {
                "from_entity_id": frm,
                "to_entity_id": to,
                "relation_type": rtype,
                "relation_raw": str(payload.get("relation_raw") or span),
            },
            {"from_entity_id": frm, "relation_type": rtype, "to_entity_id": to},
        )
    if ctype == "state":
        subj = resolve(str(payload.get("subject_entity_id") or ""))
        field = str(payload.get("field") or "")
        if not subj or not field:
            return None, None
        return (
            {
                "field": field,
                "value": payload.get("value"),
                "raw_value": str(payload.get("raw_value") or span),
                "subject_entity_id": subj,
            },
            {"subject_entity_id": subj, "field": field},
        )
    if ctype == "event":
        etype = str(payload.get("event_type") or "")
        if not etype:
            return None, None
        participants = [resolve(str(p)) for p in (payload.get("participants") or []) if p]
        loc = payload.get("location_entity_id")
        loc_resolved = resolve(str(loc)) if loc else None
        return (
            {
                "event_type": etype,
                "summary": str(payload.get("summary") or span),
                "location_entity_id": loc_resolved,
            },
            {
                "event_type": etype,
                "participants": participants,
                "location_entity_id": loc_resolved,
                "sequence_in_chapter": 1,
            },
        )
    if ctype == "org":
        org = resolve(str(payload.get("org_entity_id") or ""))
        member = resolve(str(payload.get("member_entity_id") or ""))
        role = str(payload.get("role") or "")
        if not org or not member:
            return None, None
        return (
            {
                "org_entity_id": org,
                "member_entity_id": member,
                "role": role,
                "action": str(payload.get("action") or "join"),
            },
            {"org_entity_id": org, "member_entity_id": member, "role": role},
        )
    if ctype == "term_definition":
        term = str(payload.get("term_id") or "")
        if not term:
            return None, None
        return (
            {"term_id": term, "definition": str(payload.get("definition") or span)},
            {"term_id": term},
        )
    return None, None


def build_map_extractor(settings: AppSettings | None = None) -> MapClaimExtractor:
    """按应用配置构造生产 LLM Map 抽取器（正式压缩评测 CLI 用）。

    复用阶段 06 Map 流水线：GenerationClient（httpx + tenacity 重试）
    + 版本化 prompt + 窗口分段 + Draft 校验；token 计量经配置 tokenizer。

    未配置 LLM（NOVELCANON_LLM_MODEL 为空）→ 抛清晰错误：正式压缩评测
    不得静默回退 golden-replay（复审 P0）。
    """
    from novelcanon.extraction.map_pipeline import build_map_process_fn
    from novelcanon.generation.client import GenerationClient
    from novelcanon.generation.prompts import MapPrompts
    from novelcanon.retrieval.tokenizer import FakeTokenizer

    settings = settings or AppSettings()
    if not settings.llm_model:
        raise RuntimeError(
            "正式压缩评测需要 LLM 配置：请设置 NOVELCANON_LLM_MODEL /"
            " NOVELCANON_LLM_BASE_URL / NOVELCANON_LLM_API_KEY（或 .env）。"
            "未配置 LLM 时无法衡量真实抽取 recall，不得回退 golden-replay。"
        )
    profile = GenerationProfile(
        profile_id="eval-llm-map",
        context_window=settings.llm_context_window,
        max_output_tokens=settings.llm_max_output,
        structured_output_mode=settings.llm_mode,
        tokenizer_id=settings.llm_tokenizer,
        provider=settings.llm_provider,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key_env="NOVELCANON_LLM_API_KEY",
        concurrency_limit=4,
        requests_per_minute=60,
        timeout_seconds=60.0,
        max_retries=3,
        retry_policy="exponential",
    )
    tokenizer = FakeTokenizer()
    client = GenerationClient(profile, tokenizer=tokenizer, api_key=settings.llm_api_key or None)
    process_fn = build_map_process_fn(
        book_id="<compression-eval>",
        profile=profile,
        prompts=MapPrompts(),
        tokenizer=tokenizer,
        client=client,
    )
    return MapClaimExtractor(process_fn, profile_id=profile.profile_id)
