"""压缩决策门与压缩管线（阶段 11 压缩实验 §4 / 产物）。

- gate.py：决策门——只有压缩管线事实 recall 相对原文线下降 ≤ 0.02、
  证据复现和关键 QA 不退化、且成本收益足够明确时，才允许默认启用；
  硬前置：后验全部通过 + 生产抽取模式（llm-map/production-map，
  golden-replay 不得授权启用）；
- service.py：压缩管线（章节 → 压缩文本），记录 compression version
  （prescan + rewriter + verify 版本）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

DECISION_GATE_VERSION = "gate-v2"

# 决策门阈值（11 §4）
MAX_RECALL_DROP = 0.02
MIN_COST_SAVING = 0.1  # 成本收益足够明确：token 节省 ≥ 10%

# 授权默认启用压缩的**生产抽取模式**（阶段 11 复审 P0）：
# golden-replay 是 oracle 辅助验证（黄金 claims 在压缩文本上的确定性
# 重放），只适合 fixture——它把黄金答案直接落库，不能衡量真实抽取
# recall，因此永远不得授权默认启用压缩。
PRODUCTION_EXTRACTION_MODES = frozenset({"llm-map", "production-map"})


@dataclass(frozen=True)
class GateDecision:
    """决策门结果：是否默认启用压缩 + 逐项依据。"""

    enable: bool
    version: str = DECISION_GATE_VERSION
    reasons: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "enable": self.enable,
            "version": self.version,
            "reasons": self.reasons,
        }


class DecisionGate:
    """压缩启用决策门（11 §4）：recall/证据/QA/成本四条件。"""

    def __init__(
        self,
        *,
        max_recall_drop: float = MAX_RECALL_DROP,
        min_cost_saving: float = MIN_COST_SAVING,
    ) -> None:
        self._max_recall_drop = max_recall_drop
        self._min_cost_saving = min_cost_saving

    def decide(
        self,
        baseline: dict,
        compressed: dict,
        *,
        cost_saving: float,
    ) -> GateDecision:
        """baseline/compressed：Pilot 报告的 structured 路线指标 dict。

        硬前置（阶段 11 复审）：
        - validation：所有章节 passed_validation 必须为 True（后验失败直
          接拒绝，不再评估其余条件）；
        - extraction_mode：必须为生产抽取模式（llm-map / production-map）。
          golden-replay 是 oracle 辅助验证（把黄金答案落库），永远不得
          授权默认启用压缩——即使四条件全满足也直接拒绝。
        """
        reasons: dict[str, bool] = {}
        validated = bool(compressed.get("validated", True))
        reasons["validation"] = validated
        if not validated:
            # 后验失败：压缩产物不可信，直接拒绝，不再评估其余条件
            return GateDecision(
                enable=False,
                reasons={
                    **reasons,
                    "cost_saving": round(cost_saving, 4),
                },
            )

        mode = str(compressed.get("extraction_mode") or "")
        mode_ok = mode in PRODUCTION_EXTRACTION_MODES
        reasons["extraction_mode"] = mode_ok
        if not mode_ok:
            # 非生产抽取模式：无法衡量真实抽取 recall，直接拒绝
            return GateDecision(
                enable=False,
                reasons={
                    **reasons,
                    "extraction_mode_detail": (
                        f"非生产抽取模式 {mode!r}（需要 llm-map / production-map；"
                        "golden-replay 不得授权启用压缩）"
                    ),
                    "cost_saving": round(cost_saving, 4),
                },
            )

        b_facts = baseline.get("facts") or {}
        c_facts = compressed.get("facts") or {}
        b_recall = b_facts.get("recall", 0.0)
        c_recall = c_facts.get("recall", 0.0)
        recall_ok = (b_recall - c_recall) <= self._max_recall_drop
        reasons["recall"] = recall_ok

        b_evidence = (baseline.get("evidence_reproduction") or {}).get("reproduction_rate", 0.0)
        c_evidence = (compressed.get("evidence_reproduction") or {}).get("reproduction_rate", 0.0)
        evidence_ok = c_evidence >= b_evidence
        reasons["evidence"] = evidence_ok

        b_qa = (baseline.get("qa_chapter_accuracy") or {}).get("accuracy", 0.0)
        c_qa = (compressed.get("qa_chapter_accuracy") or {}).get("accuracy", 0.0)
        qa_ok = c_qa >= b_qa
        reasons["qa"] = qa_ok

        cost_ok = cost_saving >= self._min_cost_saving
        reasons["cost"] = cost_ok

        enable = all(reasons.values())
        return GateDecision(
            enable=enable,
            reasons={
                **reasons,
                "baseline_recall": round(b_recall, 4),
                "compressed_recall": round(c_recall, 4),
                "recall_drop": round(max(0.0, b_recall - c_recall), 4),
                "cost_saving": round(cost_saving, 4),
            },
        )
