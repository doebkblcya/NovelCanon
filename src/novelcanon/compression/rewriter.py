"""受约束改写与后验校验（阶段 11 压缩实验 §2–§3）。

- rewriter.py：每段输出 keep / rewrite / drop / reason / ref_seg；
  确定性实现（无模型）保留含保留词/数字/时间的句子并截断，LLM 实现
  结构化输出；
- verify.py：后验校验——实体、数字、时间与豁免词覆盖率；低于阈值
  的段自动回退原文（11 §3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from novelcanon.compression.prescan import KeepDict
from novelcanon.config.hash import stable_config_hash

REWRITER_VERSION = "rewriter-v1"
VERIFY_VERSION = "verify-v1"

# 后验阈值（11 §3）：覆盖率低于此值的段回退原文
COVERAGE_THRESHOLD = 0.9


@dataclass(frozen=True)
class SegDecision:
    """一段的改写决策（11 §2：keep/rewrite/drop/reason/ref_seg）。"""

    seg_index: int
    action: str  # keep | rewrite | drop
    reason: str
    ref_seg: str  # 稳定段落引用（原文 span 的 hash）
    output_text: str = ""


@dataclass(frozen=True)
class CompressionResult:
    """一章的压缩结果：逐段决策 + 拼接文本 + 保留率。"""

    chapter_id: str
    segments: list[SegDecision] = field(default_factory=list)
    output_text: str = ""
    retention: float = 0.0
    version: str = ""

    @property
    def dropped(self) -> int:
        return sum(1 for s in self.segments if s.action == "drop")


class Rewriter:
    """受约束改写抽象：输入段落 + 保留词典，输出逐段决策。"""

    def rewrite(self, seg_index: int, text: str, keep: KeepDict) -> SegDecision:
        raise NotImplementedError


class DeterministicRewriter(Rewriter):
    """无模型改写：保留含保留词/数字/时间的段落，纯描述句 drop。"""

    def rewrite(self, seg_index: int, text: str, keep: KeepDict) -> SegDecision:
        from novelcanon.compression.prescan import _CN_NUM_RE, _TIME_RE

        terms = keep.all_terms()
        has_keep = any(t in text for t in terms if len(t) >= 2)
        has_number = bool(_CN_NUM_RE.search(text))
        has_time = bool(_TIME_RE.search(text))
        if has_keep or has_number or has_time:
            out = text
            action = "keep"
            reason = "含保留词/数字/时间"
        else:
            out = ""
            action = "drop"
            reason = "无保留词，推断性描述"
        return SegDecision(
            seg_index=seg_index,
            action=action,
            reason=reason,
            ref_seg=stable_hash(text),
            output_text=out,
        )


class LLMRewriter(Rewriter):
    """模型受约束改写：prompt 结构化输出 keep/rewrite/drop/reason/ref_seg。"""

    def __init__(
        self,
        client,
        *,
        profile_id: str = "",
        prompt_version: str = "llm-rewrite-v1",
    ) -> None:
        self._client = client
        self.profile_id = profile_id or getattr(client, "profile_id", "")
        self.prompt_version = prompt_version

    def rewrite(self, seg_index: int, text: str, keep: KeepDict) -> SegDecision:
        import asyncio
        import json

        prompt = (
            "你是 NovelCanon 压缩改写器。必须逐字保留以下词典中的词项："
            f"{sorted(keep.all_terms())}\n"
            '对给定段落输出 JSON：{"action": "keep|rewrite|drop",'
            ' "reason": 原因, "text": 改写后文本}。'
            "rewrite 时保留全部保留词与数字/时间表达，删去修饰性内容。\n"
            f"段落：{text}"
        )
        result = asyncio.run(self._client.complete(prompt))
        try:
            payload = json.loads(result.raw_text)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        action = payload.get("action", "keep")
        if action not in ("keep", "rewrite", "drop"):
            action = "keep"
        out = payload.get("text") if action == "rewrite" else (text if action == "keep" else "")
        return SegDecision(
            seg_index=seg_index,
            action=action,
            reason=str(payload.get("reason") or ""),
            ref_seg=stable_hash(text),
            output_text=str(out or ""),
        )


def stable_hash(text: str) -> str:
    from novelcanon.config.hash import stable_config_hash

    return stable_config_hash({"seg": text})


class Compressor:
    """按段压缩整章：split → 逐段改写 → 拼接。"""

    def __init__(
        self,
        rewriter: Rewriter | None = None,
        *,
        version: str = REWRITER_VERSION,
    ) -> None:
        self._rewriter = rewriter or DeterministicRewriter()
        self._version = version

    def compress(self, chapter_id: str, text: str, keep: KeepDict) -> CompressionResult:
        segments = [s for s in text.split("\n\n") if s.strip()] or [text]
        decisions = [self._rewriter.rewrite(i, seg, keep) for i, seg in enumerate(segments)]
        output = "\n\n".join(d.output_text for d in decisions if d.output_text)
        original_len = len(text) or 1
        retention = round(len(output) / original_len, 4)
        return CompressionResult(
            chapter_id=chapter_id,
            segments=decisions,
            output_text=output,
            retention=retention,
            version=stable_config_hash({"rewriter": self._version, "keep": keep.version}),
        )


class PostValidator:
    """后验校验（11 §3）：实体/数字/时间/豁免词覆盖率 + 自动回退。

    - 逐段校验：非 drop 段须实体、数字、时间覆盖率均达标，任一低于
      阈值即回退原文；
    - drop 兜底：被 drop 的段落若仍含专名/数字/时间（LLM 错删），
      强制回退原文（P0：错删关键内容不得永久丢失）；
    - 整章 passed：综合覆盖率 + 数字覆盖率 + 时间覆盖率均达标且无
      被回退的 drop 段。
    """

    def __init__(self, *, threshold: float = COVERAGE_THRESHOLD) -> None:
        self._threshold = threshold

    def validate(self, original: str, result: CompressionResult, keep: KeepDict) -> dict:
        """校验压缩结果；覆盖率低于阈值的段回退原文。"""
        from novelcanon.compression.prescan import _CN_NUM_RE, _TIME_RE

        terms = keep.all_terms()
        # 段列表与 Compressor 一致（过滤空段，seg_index 对齐）
        segments = [s for s in original.split("\n\n") if s.strip()] or [original]

        def seg_text(d: SegDecision) -> str:
            if 0 <= d.seg_index < len(segments):
                return segments[d.seg_index]
            return original

        def has_keep_content(seg: str) -> bool:
            """段内是否存在必须保留的内容（专名/数字/时间）。"""
            return (
                any(t in seg for t in terms if len(t) >= 2)
                or bool(_CN_NUM_RE.search(seg))
                or bool(_TIME_RE.search(seg))
            )

        def needs_fallback(d: SegDecision) -> bool:
            """非 drop 段：实体/数字/时间任一覆盖率低于阈值即回退。"""
            if d.action == "drop":
                return False
            seg = seg_text(d)
            out = d.output_text or seg
            return (
                self._segment_coverage(seg, out, terms) < self._threshold
                or self._token_coverage(seg, out, _CN_NUM_RE.findall) < self._threshold
                or self._token_coverage(seg, out, _TIME_RE.findall) < self._threshold
            )

        # 逐段回退决策
        final_segments: list[str] = []
        fallback_count = 0
        restored_drops = 0
        for d in result.segments:
            if d.action == "drop":
                # drop 兜底：段内仍含专名/数字/时间 → 错删，回退原文
                if has_keep_content(seg_text(d)):
                    final_segments.append(seg_text(d))
                    fallback_count += 1
                    restored_drops += 1
                continue
            out = d.output_text or seg_text(d)
            if needs_fallback(d):
                out = seg_text(d)  # 回退原文
                fallback_count += 1
            final_segments.append(out)
        output = "\n\n".join(final_segments)

        coverage = self._coverage(original, output, terms)
        number_cover = self._token_coverage(original, output, lambda t: _CN_NUM_RE.findall(t))
        time_cover = self._token_coverage(original, output, lambda t: _TIME_RE.findall(t))
        passed = (
            coverage >= self._threshold
            and number_cover >= self._threshold
            and time_cover >= self._threshold
            and restored_drops == 0
        )
        return {
            "coverage": round(coverage, 4),
            "number_coverage": round(number_cover, 4),
            "time_coverage": round(time_cover, 4),
            "fallback_segments": fallback_count,
            "restored_drop_segments": restored_drops,
            "output_text": output,
            "passed": passed,
        }

    @staticmethod
    def _segment_coverage(original: str, out: str, terms: frozenset[str]) -> float:
        if not original:
            return 1.0
        kept = sum(1 for t in terms if len(t) >= 2 and t in original and t in out)
        total = sum(1 for t in terms if len(t) >= 2 and t in original)
        return kept / total if total else 1.0

    @staticmethod
    def _coverage(original: str, out: str, terms: frozenset[str]) -> float:
        return PostValidator._segment_coverage(original, out, terms)

    @staticmethod
    def _token_coverage(original: str, out: str, extract) -> float:
        orig_tokens = set(extract(original))
        if not orig_tokens:
            return 1.0
        kept = {t for t in orig_tokens if t in out}
        return len(kept) / len(orig_tokens)
