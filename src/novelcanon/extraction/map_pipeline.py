"""逐章 Map 流水线组装（阶段 06，docs/implementation/06）。

把 GenerationClient + 版本化 prompt + 窗口分段 + Draft 校验串成 runner 的
process_fn：worker 只产出通过校验的 ExtractionDraftV1（或结构化错误），
staging 由 runner 的 writer 批量事务写入（single writer 原则）。

重试策略（06 §4）：
- 传输/限流/服务端错误：client 层 tenacity 指数退避；
- Schema 错误（parse/Pydantic）：每段最多 max_repair_attempts 次结构修复
  请求（附错误信息重发）；
- 确定性契约错误（ID 引用、ref 范围、披露边界、不变量）不重试，直判失败。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from novelcanon.config.settings import GenerationProfile
from novelcanon.generation.client import (
    GenerationResult,
    request_hash,
    response_hash,
)
from novelcanon.generation.parser import DraftValidator, Issue, parse_response
from novelcanon.generation.prompts import MapPrompts, build_map_prompt
from novelcanon.generation.segments import (
    SourceSegment,
    build_ref_segments,
    ref_segment_prompt_lines,
    split_for_window,
)
from novelcanon.pipeline.ledger import Usage
from novelcanon.pipeline.runner import ChapterTask, ProcessResult
from novelcanon.retrieval.tokenizer import Tokenizer
from novelcanon.schemas.draft import ExtractionDraftV1, RefSourceSegment

# prompt 骨架（system/schema/指令）的固定 token 开销余量
_PROMPT_OVERHEAD_TOKENS = 500


class MapClient(Protocol):
    """process_fn 需要的 provider 接口（GenerationClient / FakeGenerationClient）。"""

    async def complete(self, prompt: str) -> GenerationResult: ...


@dataclass(frozen=True)
class _SegmentPart:
    parsed: dict | None
    issues: list[Issue]
    request_hash: str
    response_hash: str
    raw_text: str
    usage: Usage


def build_map_process_fn(
    *,
    book_id: str,
    profile: GenerationProfile,
    prompts: MapPrompts,
    tokenizer: Tokenizer,
    client: MapClient,
    max_repair_attempts: int = 1,
) -> Callable[[ChapterTask], Awaitable[ProcessResult]]:
    """构造单章 Map 的 process_fn（每章一次或按窗口多次调用，产出一份 Draft）。"""

    window_tokens = max(
        1, profile.context_window - profile.max_output_tokens - _PROMPT_OVERHEAD_TOKENS
    )

    async def _request_segment(
        seg: SourceSegment,
        ref_lines: list[str],
        chapter_id: str,
        ordinal: int,
        repair_issues: list[str] | None = None,
    ) -> _SegmentPart:
        usage = Usage()
        prompt = build_map_prompt(
            prompts,
            seg.content,
            book_id=book_id,
            chapter_id=chapter_id,
            chapter_ordinal=ordinal,
            ref_segment_lines=ref_lines,
            repair_issues=repair_issues,  # 阶段 11 增强 A：逐字引用修复请求
        )
        req_hash = request_hash(prompt, model=profile.model, profile_id=profile.profile_id)
        result: GenerationResult = await client.complete(prompt)
        usage = usage + result.usage
        parsed, issues = parse_response(result.raw_text)
        for _ in range(max_repair_attempts):
            if parsed is not None:
                break
            # 结构修复请求：附上次错误重发（06 §4：Schema 错误有限次修复）
            repair_prompt = build_map_prompt(
                prompts,
                seg.content,
                book_id=book_id,
                chapter_id=chapter_id,
                chapter_ordinal=ordinal,
                ref_segment_lines=ref_lines,
                repair_issues=[i.message for i in issues],
            )
            req_hash = request_hash(
                repair_prompt, model=profile.model, profile_id=profile.profile_id
            )
            result = await client.complete(repair_prompt)
            usage = usage + result.usage
            parsed, issues = parse_response(result.raw_text)
        return _SegmentPart(
            parsed=parsed,
            issues=issues,
            request_hash=req_hash,
            response_hash=response_hash(result.raw_text),
            raw_text=result.raw_text,
            usage=usage,
        )

    def _merge(parts: list[_SegmentPart], refs: list[RefSourceSegment]) -> dict:
        first = next((p.parsed for p in parts if p.parsed is not None), None) or {}
        merged: dict = {
            "book_id": first.get("book_id", book_id),
            "chapter_id": first.get("chapter_id", ""),
            "chapter_ordinal": first.get("chapter_ordinal", 0),
            "mentions": [],
            "local_events": [],
            "provisional_claims": [],
            "ref_source_segments": [r.model_dump() for r in refs],
            "local_causes": [],
            "cause_candidates": [],
            "unresolved": [],
        }
        for key in (
            "mentions",
            "local_events",
            "provisional_claims",
            "local_causes",
            "cause_candidates",
            "unresolved",
        ):
            for part in parts:
                if part.parsed is not None:
                    merged[key].extend(part.parsed.get(key, []))
        return merged

    async def process(task: ChapterTask) -> ProcessResult:
        segments = split_for_window(task.content, tokenizer, window_tokens)
        refs = build_ref_segments(task.chapter_id, segments)
        ref_line_by_seg = {
            seg.segment_id: line
            for seg, line in zip(
                segments,
                ref_segment_prompt_lines(task.chapter_id, segments),
                strict=True,
            )
        }

        parts = await asyncio.gather(
            *[
                _request_segment(
                    seg, [ref_line_by_seg[seg.segment_id]], task.chapter_id, task.ordinal
                )
                for seg in segments
            ]
        )

        total_usage = Usage()
        for part in parts:
            total_usage = total_usage + part.usage

        if any(p.parsed is None for p in parts):
            issues = [i for p in parts for i in p.issues] or [Issue("parse_error", "响应解析失败")]
            return ProcessResult(
                payload=_invalid_payload(issues, parts),
                usage=total_usage,
                failed=True,
                error=f"Map 响应解析失败：{issues[0].message}",
            )

        merged = _merge(parts, refs)
        validator = DraftValidator(
            book_id=book_id,
            chapter_id=task.chapter_id,
            chapter_ordinal=task.ordinal,
            chapter_text=task.content,
        )
        draft, issues = validator.validate(merged)
        quote_issues: list[Issue] = []

        # 阶段 11 增强 A 第 3 项：draft 结构通过后，立即检查逐字字段
        # （summary/relation_raw/raw_value/clue_anchor/definition 是否在原文
        # 中可定位）。改写句若等到证据物化才丢弃（no_span_found 62.6%），
        # 成本已发生且无法修复——这里在 Map 阶段触发**一次**针对性 repair，
        # 把逐字缺失项作为 repair_issues 重发。
        if draft is not None:
            from novelcanon.generation.parser import LiteralQuoteCheck

            # P1（十三轮）：按引用段校验（与证据对齐同范围）——多窗口章节
            # 中引用文字若只在其他段，Map 阶段不得误通过。
            segment_text_by_id = {seg.segment_id: seg.content for seg in segments}
            quote_check = LiteralQuoteCheck(task.content, segment_text_by_id)
            quote_issues = quote_check.check(merged)
            if quote_issues and max_repair_attempts >= 1:
                # 只重发有逐字问题的 segment（claims 按 ref_source_segment_id
                # 归属段；无引用段的按全部段重发以兜底）。
                affected_segs: set[str] = set()
                for issue in quote_issues:
                    for claim in merged.get("provisional_claims") or []:
                        if isinstance(claim, dict) and issue.message.startswith(
                            f"claim {claim.get('provisional_claim_id')}"
                        ):
                            seg_id = claim.get("ref_source_segment_id")
                            if isinstance(seg_id, str) and seg_id:
                                affected_segs.add(seg_id)
                repair_segs = (
                    [s for s in segments if s.segment_id in affected_segs]
                    if affected_segs
                    else segments
                )
                # P1（十三轮）：不得重置 total_usage——首次调用的 token/成本
                # 必须保留，repair 响应 usage 累加其上（否则账本只记 repair，
                # 正式 Pilot 每万字 token/重试成本偏低）。
                repaired_usage = Usage()
                repaired_parts = await asyncio.gather(
                    *[
                        _request_segment(
                            seg,
                            [ref_line_by_seg[seg.segment_id]],
                            task.chapter_id,
                            task.ordinal,
                            repair_issues=[i.message for i in quote_issues],
                        )
                        for seg in repair_segs
                    ]
                )
                for part in repaired_parts:
                    repaired_usage = repaired_usage + part.usage
                total_usage = total_usage + repaired_usage
                if any(p.parsed is None for p in repaired_parts):
                    issues = [i for p in repaired_parts for i in p.issues] or [
                        Issue("parse_error", "逐字修复响应解析失败")
                    ]
                    return ProcessResult(
                        payload=_invalid_payload(issues, parts),
                        usage=total_usage,
                        failed=True,
                        error=f"Map 逐字修复响应解析失败：{issues[0].message}",
                    )
                # 替换受影响段的结果；未重发的段保留首次结果
                repaired_by_seg = {
                    seg.segment_id: part
                    for seg, part in zip(repair_segs, repaired_parts, strict=True)
                }
                parts = [
                    repaired_by_seg.get(seg.segment_id, part)
                    for seg, part in zip(segments, parts, strict=True)
                ]
                merged = _merge(parts, refs)
                draft, issues = validator.validate(merged)
                if draft is not None:
                    quote_issues = quote_check.check(merged)

        if draft is not None:
            return ProcessResult(
                payload=_valid_payload(draft, parts, quote_issues=quote_issues),
                usage=total_usage,
            )
        return ProcessResult(
            payload=_invalid_payload(issues, parts),
            usage=total_usage,
            failed=True,
            error=f"Draft 校验失败：{issues[0].message}",
        )

    return process


def _combine_request_hashes(parts: list[_SegmentPart]) -> str:
    """多段请求的聚合 hash：全部段的 request hash 稳定聚合（06 修复：
    不再只保存最后一组；请求与响应分开聚合，语义可区分审计）。"""
    from novelcanon.config.hash import stable_config_hash

    return stable_config_hash({"requests": [p.request_hash for p in parts]})


def _combine_response_hashes(parts: list[_SegmentPart]) -> str:
    """多段响应的聚合 hash：全部段的 response hash 稳定聚合。

    与 _combine_request_hashes 分离（验收 P1）：request_hash 与
    response_hash 各自聚合自己的内容，不再复用同一个聚合函数。
    """
    from novelcanon.config.hash import stable_config_hash

    return stable_config_hash({"responses": [p.response_hash for p in parts]})


def _combine_raw(parts: list[_SegmentPart]) -> str:
    """多段响应摘要（完整原文过大，保存每段 hash + 前 200 字符）。"""
    return "\n---\n".join(f"[{p.response_hash[:12]}…] {p.raw_text[:200]}" for p in parts)


def _valid_payload(
    draft: ExtractionDraftV1,
    parts: list[_SegmentPart],
    quote_issues: list[Issue] | None = None,
) -> dict:
    # 阶段 11 增强 A：repair 后仍无法逐字定位的字段记录为 warning
    # （不拒绝 draft——证据层会如实丢弃；记入审计便于统计收敛情况）。
    return {
        "draft": draft.model_dump(mode="json"),
        "request_hash": _combine_request_hashes(parts),
        "response_hash": _combine_response_hashes(parts),
        "validation_issues": (
            [{"code": i.code, "message": i.message} for i in quote_issues] if quote_issues else []
        ),
        "status": "valid",
        "raw_response": _combine_raw(parts),
    }


def _invalid_payload(issues: list[Issue], parts: list[_SegmentPart]) -> dict:
    return {
        "draft": None,
        "request_hash": _combine_request_hashes(parts),
        "response_hash": _combine_response_hashes(parts),
        "validation_issues": [{"code": i.code, "message": i.message} for i in issues],
        "status": "invalid",
        "error_summary": issues[0].message if issues else None,
        "raw_response": _combine_raw(parts),
    }
