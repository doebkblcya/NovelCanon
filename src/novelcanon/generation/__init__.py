"""Generation provider 适配器（阶段 06，docs/implementation/06）。

版本化 Map prompt、窗口分段与 ref 映射、7 层 Draft 校验、
httpx+tenacity 真实调用（Fake 无网络基线）、按类型统计抽取报告。
"""

from novelcanon.generation.client import (
    FakeGenerationClient,
    GenerationClient,
    GenerationResult,
    request_hash,
    resolve_api_key,
    response_hash,
)
from novelcanon.generation.parser import DraftValidator, Issue, parse_response
from novelcanon.generation.prompts import (
    MapPrompts,
    build_map_prompt,
    default_map_prompts,
    schema_for_draft,
)
from novelcanon.generation.report import extraction_report
from novelcanon.generation.segments import (
    SourceSegment,
    build_ref_segments,
    ref_segment_prompt_lines,
    split_for_window,
)

__all__ = [
    "DraftValidator",
    "FakeGenerationClient",
    "GenerationClient",
    "GenerationResult",
    "Issue",
    "MapPrompts",
    "SourceSegment",
    "build_map_prompt",
    "build_ref_segments",
    "default_map_prompts",
    "extraction_report",
    "parse_response",
    "ref_segment_prompt_lines",
    "request_hash",
    "resolve_api_key",
    "response_hash",
    "schema_for_draft",
    "split_for_window",
]
