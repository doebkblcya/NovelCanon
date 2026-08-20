"""压缩（阶段 11 压缩实验，docs/implementation/11 §压缩实验）。

规则预扫描（prescan.py）→ 受约束改写（rewriter.py）→ 后验校验
（verify）→ 决策门（gate.py）→ 压缩管线（service.py）。
"""

from novelcanon.compression.gate import DECISION_GATE_VERSION, DecisionGate, GateDecision
from novelcanon.compression.prescan import KeepDict, Prescanner
from novelcanon.compression.rewriter import (
    CompressionResult,
    Compressor,
    DeterministicRewriter,
    LLMRewriter,
    PostValidator,
    Rewriter,
    SegDecision,
)
from novelcanon.compression.service import (
    COMPRESSION_PIPELINE_VERSION,
    ChapterCompression,
    CompressionService,
    decide_compression,
)

__all__ = [
    "COMPRESSION_PIPELINE_VERSION",
    "ChapterCompression",
    "CompressionResult",
    "CompressionService",
    "Compressor",
    "DECISION_GATE_VERSION",
    "DecisionGate",
    "DeterministicRewriter",
    "GateDecision",
    "KeepDict",
    "LLMRewriter",
    "PostValidator",
    "Prescanner",
    "Rewriter",
    "SegDecision",
    "decide_compression",
]
