"""压缩管线（阶段 11 压缩实验，docs/implementation/11）。

CompressionService：章节文本 → 规则预扫描 → 受约束改写 → 后验校验 →
压缩文本；compression version 由 prescan + rewriter + verify 版本聚合
（可追踪、可失效）。
"""

from __future__ import annotations

from dataclasses import dataclass

from novelcanon.compression.gate import DecisionGate, GateDecision
from novelcanon.compression.prescan import KeepDict, Prescanner
from novelcanon.compression.rewriter import (
    VERIFY_VERSION,
    CompressionResult,
    Compressor,
    PostValidator,
    Rewriter,
)
from novelcanon.config.hash import stable_config_hash

COMPRESSION_PIPELINE_VERSION = "compression-v1"


@dataclass(frozen=True)
class ChapterCompression:
    """一章的压缩产物（含版本与校验结果）。"""

    chapter_id: str
    original_text: str
    compressed_text: str
    keep: KeepDict
    result: CompressionResult
    validation: dict
    compression_version: str

    @property
    def retention(self) -> float:
        return self.result.retention

    @property
    def passed_validation(self) -> bool:
        return bool(self.validation.get("passed"))


class CompressionService:
    """压缩管线：prescan → rewrite → verify（每章）。"""

    def __init__(
        self,
        prescanner: Prescanner | None = None,
        rewriter: Rewriter | None = None,
        validator: PostValidator | None = None,
        *,
        pipeline_version: str = COMPRESSION_PIPELINE_VERSION,
    ) -> None:
        self._prescanner = prescanner or Prescanner()
        self._compressor = Compressor(rewriter=rewriter)
        self._validator = validator or PostValidator()
        self._pipeline_version = pipeline_version

    def compress_chapter(
        self,
        chapter_id: str,
        text: str,
        *,
        known_surfaces: list[str] | None = None,
        keep: KeepDict | None = None,
    ) -> ChapterCompression:
        """压缩一章：共享词典可预扫描一次后复用（多章）。"""
        if keep is None:
            keep = self._prescanner.scan([text], known_surfaces=known_surfaces or [])
        result = self._compressor.compress(chapter_id, text, keep)
        validation = self._validator.validate(text, result, keep)
        compressed = validation.get("output_text") or result.output_text
        version = stable_config_hash(
            {
                "pipeline": self._pipeline_version,
                "keep": keep.version,
                "rewriter": result.version,
                "verify": VERIFY_VERSION,
            }
        )
        return ChapterCompression(
            chapter_id=chapter_id,
            original_text=text,
            compressed_text=compressed,
            keep=keep,
            result=result,
            validation=validation,
            compression_version=version,
        )

    def compress_book(
        self,
        texts: list[tuple[str, str]],
        *,
        known_surfaces: list[str] | None = None,
    ) -> list[ChapterCompression]:
        """压缩整本（先预扫描全书词典，再逐章压缩）。"""
        keep = self._prescanner.scan([t for _, t in texts], known_surfaces=known_surfaces or [])
        return [self.compress_chapter(cid, text, keep=keep) for cid, text in texts]


def decide_compression(baseline: dict, compressed: dict, *, cost_saving: float) -> GateDecision:
    """决策门入口（11 §4）：Pilot structured 路线指标对比。"""
    return DecisionGate().decide(baseline, compressed, cost_saving=cost_saving)
