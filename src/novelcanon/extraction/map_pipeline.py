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
        seg: SourceSegment, ref_lines: list[str], chapter_id: str, ordinal: int
    ) -> _SegmentPart:
        usage = Usage()
        prompt = build_map_prompt(
            prompts,
            seg.content,
            book_id=book_id,
            chapter_id=chapter_id,
            chapter_ordinal=ordinal,
            ref_segment_lines=ref_lines,
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

        parts = await asyncio.gather(*[
            _request_segment(seg, [ref_line_by_seg[seg.segment_id]], task.chapter_id, task.ordinal)
            for seg in segments
        ])

        total_usage = Usage()
        for part in parts:
            total_usage = total_usage + part.usage

        last = parts[-1]
        if any(p.parsed is None for p in parts):
            issues = [i for p in parts for i in p.issues] or [
                Issue("parse_error", "响应解析失败")
            ]
            return ProcessResult(
                payload=_invalid_payload(issues, last),
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
        if draft is not None:
            return ProcessResult(
                payload=_valid_payload(draft, last),
                usage=total_usage,
            )
        return ProcessResult(
            payload=_invalid_payload(issues, last),
            usage=total_usage,
            failed=True,
            error=f"Draft 校验失败：{issues[0].message}",
        )

    return process


def _valid_payload(draft: ExtractionDraftV1, last: _SegmentPart) -> dict:
    return {
        "draft": draft.model_dump(mode="json"),
        "request_hash": last.request_hash,
        "response_hash": last.response_hash,
        "validation_issues": [],
        "status": "valid",
        "raw_response": last.raw_text,
    }


def _invalid_payload(issues: list[Issue], last: _SegmentPart) -> dict:
    return {
        "draft": None,
        "request_hash": last.request_hash,
        "response_hash": last.response_hash,
        "validation_issues": [{"code": i.code, "message": i.message} for i in issues],
        "status": "invalid",
        "error_summary": issues[0].message if issues else None,
        "raw_response": last.raw_text,
    }
