"""Draft parser / validator（阶段 06，docs/implementation/06 §3）。

响应校验按以下层次拒绝错误（06 §3）：
1. 结构化输出解析（JSON 解析，容忍 Markdown 围栏）；
2. JSON/类型 Schema（Pydantic，extra=forbid 拒绝 canonical_id 等越界字段）；
3. ID 仅引用当前 Draft 内对象（mention / local_event / provisional_claim
   唯一 + participants / local_causes / ref_source_segment 引用可解析）；
4. 枚举与必填字段（Pydantic 枚举 + 基础必填）；
5. ref_source_segment 范围（段引用存在且 char_offset 落在对应原文区间）；
6. 本章披露边界（无 canonical_id / 最终 event ID / 跨章事件链接）；
7. 基础业务不变量（book_id / chapter_id / ordinal 与输入一致）。

无效输出以 Issue(code, message) 结构化返回，code 供抽取报告按类型统计。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from novelcanon.schemas.draft import ExtractionDraftV1

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


@dataclass(frozen=True)
class Issue:
    """一条校验问题；code 为稳定分类（报告统计用）。"""

    code: str
    message: str


def _strip_fence(raw: str) -> str:
    return _JSON_FENCE.sub("", raw.strip())


def parse_response(raw_text: str) -> tuple[dict[str, Any] | None, list[Issue]]:
    """第 1 层：JSON 解析（容忍围栏）。失败返回 (None, [parse_error])。"""
    text = _strip_fence(raw_text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [
            Issue(
                "parse_error",
                f"响应不是合法 JSON：{exc.msg}（位置 {exc.pos}）",
            )
        ]
    if not isinstance(parsed, dict):
        return None, [Issue("parse_error", "响应 JSON 必须是对象")]
    return parsed, []


class DraftValidator:
    """按 7 层校验把模型输出解析为合法 ExtractionDraftV1。

    构造参数是本章的输入侧事实（book/chapter/ordinal/原文/段），
    validate() 返回 (draft | None, issues)。
    """

    def __init__(
        self,
        *,
        book_id: str,
        chapter_id: str,
        chapter_ordinal: int,
        chapter_text: str,
    ) -> None:
        self._book_id = book_id
        self._chapter_id = chapter_id
        self._ordinal = chapter_ordinal
        self._chapter_text = chapter_text

    def validate(
        self, payload: dict[str, Any]
    ) -> tuple[ExtractionDraftV1 | None, list[Issue]]:
        issues: list[Issue] = []

        # 第 6 层（dict 级先行）：event_link 等越界 claim 在 Schema 校验前拒绝，
        # 避免 Pydantic 先报 schema_error 掩盖披露边界问题。
        issues.extend(self._disclosure_issues(payload))

        # 第 2 层：Schema（extra=forbid 拒绝 canonical_id 等越界字段）
        try:
            draft = ExtractionDraftV1.model_validate(payload)
        except ValidationError as exc:
            issues.append(
                Issue(
                    "schema_error",
                    f"Draft Schema 校验失败：{_first_validation_error(exc)}",
                )
            )
            return None, issues

        # 第 7 层：基础业务不变量
        if draft.book_id != self._book_id:
            issues.append(
                Issue(
                    "invariant",
                    f"book_id={draft.book_id} 与输入 {self._book_id} 不一致",
                )
            )
        if draft.chapter_id != self._chapter_id:
            issues.append(
                Issue(
                    "invariant",
                    f"chapter_id={draft.chapter_id} 与输入 {self._chapter_id} 不一致",
                )
            )
        if draft.chapter_ordinal != self._ordinal:
            issues.append(
                Issue(
                    "invariant",
                    f"chapter_ordinal={draft.chapter_ordinal} 与输入 {self._ordinal} 不一致",
                )
            )

        # 第 3 层：ID 唯一 + 引用可解析
        issues.extend(self._id_reference_issues(draft))

        # 第 5 层：ref_source_segment 范围
        issues.extend(self._ref_range_issues(draft))

        if issues:
            return None, issues
        return draft, []

    # ── 第 3 层：ID 引用 ──────────────────────────────────────

    def _id_reference_issues(self, draft: ExtractionDraftV1) -> list[Issue]:
        issues: list[Issue] = []
        mention_ids = [m.mention_id for m in draft.mentions]
        event_ids = [e.local_event_id for e in draft.local_events]
        claim_ids = [c.provisional_claim_id for c in draft.provisional_claims]
        segment_ids = [s.segment_id for s in draft.ref_source_segments]

        for name, ids in (
            ("mention_id", mention_ids),
            ("local_event_id", event_ids),
            ("provisional_claim_id", claim_ids),
            ("segment_id", segment_ids),
        ):
            dupes = {i for i in ids if ids.count(i) > 1}
            for d in sorted(dupes):
                issues.append(Issue("id_ref", f"{name} 重复：{d}"))

        for event in draft.local_events:
            for participant in event.participants:
                if participant not in mention_ids:
                    issues.append(
                        Issue(
                            "id_ref",
                            f"事件 {event.local_event_id} 引用不存在的 mention_id："
                            f"{participant}",
                        )
                    )
        for cause in draft.local_causes:
            if cause.local_event_id not in event_ids:
                issues.append(
                    Issue(
                        "id_ref",
                        f"local_causes 引用不存在的 local_event_id：{cause.local_event_id}",
                    )
                )
        for claim in draft.provisional_claims:
            if (
                claim.ref_source_segment_id is not None
                and claim.ref_source_segment_id not in segment_ids
            ):
                issues.append(
                    Issue(
                        "id_ref",
                        f"claim {claim.provisional_claim_id} 引用不存在的"
                        f" ref_source_segment_id：{claim.ref_source_segment_id}",
                    )
                )
        return issues

    # ── 第 5 层：ref_source_segment 范围 ──────────────────────

    def _ref_range_issues(self, draft: ExtractionDraftV1) -> list[Issue]:
        issues: list[Issue] = []
        text_len = len(self._chapter_text)
        for seg in draft.ref_source_segments:
            if seg.char_offset < 0 or seg.char_offset > text_len:
                issues.append(
                    Issue(
                        "ref_range",
                        f"段 {seg.segment_id} char_offset={seg.char_offset} 越界"
                        f"（章长 {text_len}）",
                    )
                )
        return issues

    # ── 第 6 层：本章披露边界 ─────────────────────────────────

    def _disclosure_issues(self, payload: dict[str, Any]) -> list[Issue]:
        """结构性约束已由 extra=forbid 保证；此处显式双保险。"""
        issues: list[Issue] = []
        for key in ("canonical_id", "canonical_map", "final_evidence", "event_link"):
            if key in payload:
                issues.append(
                    Issue("disclosure", f"Draft 包含越界字段：{key}（Map 不得输出）")
                )
        # claim 类型为 event_link 属于跨章事件链接，Map 不得输出
        for claim in payload.get("provisional_claims", []):
            if isinstance(claim, dict) and claim.get("claim_type") == "event_link":
                issues.append(
                    Issue(
                        "disclosure",
                        f"claim {claim.get('provisional_claim_id')} 类型为 event_link，"
                        "Map 阶段不得构造跨章事件链接",
                    )
                )
        return issues


def _first_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(x) for x in first.get("loc", ()))
    return f"{loc}: {first.get('msg', '')}" if loc else str(first.get("msg", ""))
