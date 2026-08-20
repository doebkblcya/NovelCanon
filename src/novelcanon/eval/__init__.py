"""评测框架（阶段 11 P1/P5，docs/implementation/11）。

- golden.py：评测黄金集（QA 引用章节 / 实体合并对 / 事实 / 因果边）；
- metrics.py：指标计算（F1 / precision-recall / 证据复现 / QA 章节正确率）；
- pilot.py：三路线 Pilot 运行器与报告；
- extractor.py：压缩评测的真实 LLM Map 抽取器（llm-map）；
- usage.py：每万字 Token/时间/成本 + 全链路 stage 用量账本。
"""

from novelcanon.eval.golden import (
    GoldenCausal,
    GoldenQA,
    GoldenSet,
    golden_set_from_chapters,
    golden_set_from_dict,
    golden_set_from_file,
    golden_set_to_dict,
)
from novelcanon.eval.metrics import (
    Metrics,
    causal_edge_precision,
    entity_merge_metrics,
    evidence_hash_reproduction,
    fact_metrics,
    qa_chapter_accuracy,
)
from novelcanon.eval.pilot import (
    PilotReport,
    run_hybrid_qa,
    run_pilot,
    run_structured_qa,
    validate_golden_against_book,
)

__all__ = [
    "GoldenCausal",
    "GoldenQA",
    "GoldenSet",
    "golden_set_from_chapters",
    "golden_set_from_dict",
    "golden_set_from_file",
    "golden_set_to_dict",
    "Metrics",
    "PilotReport",
    "causal_edge_precision",
    "entity_merge_metrics",
    "evidence_hash_reproduction",
    "fact_metrics",
    "qa_chapter_accuracy",
    "run_hybrid_qa",
    "run_pilot",
    "run_structured_qa",
    "validate_golden_against_book",
]
