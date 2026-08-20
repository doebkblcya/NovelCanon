"""Pilot 用量账本（阶段 11 复审 P1）。

- ``StageUsage``：单个管线阶段（map / rewrite / disambiguation /
  evidence_verify / link / reduce）的输入字符量、调用数、token、耗时与
  名义成本；
- **每万字归一化**：tokens / (input_chars / 10000)，耗时与成本同理——
  跨语料规模可比（定版方案 P1「每万字 Token/时间/成本」）；
- ``pipeline_usage_ledger``：汇总各阶段 → 总量 + 每万字；
- ``usage_cost_usd``：名义成本模型（token_ledger 为权威计量）。

确定性阶段（disambiguation / evidence_verify / link / rewrite）无模型
调用时 tokens=0，成本只来自 LLM 阶段（map / reduce / 查询）——账本如实
呈现，不虚构 token。
"""

from __future__ import annotations

from dataclasses import dataclass

from novelcanon.pipeline.ledger import Usage

# 名义成本模型（P1 报告用；真实成本以 token_ledger 为准）
COST_PER_M_INPUT = 0.15  # USD / 1M input tokens
COST_PER_M_CACHED_INPUT = 0.015  # 缓存输入按 10%
COST_PER_M_OUTPUT = 0.60  # USD / 1M output（含 reasoning）


def usage_cost_usd(u: Usage) -> float:
    """一次调用计量的名义成本（USD）。"""
    return (
        (u.input_tokens / 1e6) * COST_PER_M_INPUT
        + (u.cached_input_tokens / 1e6) * COST_PER_M_CACHED_INPUT
        + ((u.output_tokens + u.reasoning_tokens) / 1e6) * COST_PER_M_OUTPUT
    )


def per_wan(value: float, input_chars: int, *, digits: int = 4) -> float:
    """每万字归一化：value / (input_chars / 10000)。"""
    if not input_chars:
        return 0.0
    return round(value / (input_chars / 10000.0), digits)


@dataclass(frozen=True)
class StageUsage:
    """一个管线阶段的用量行（每万字可归一化）。"""

    stage: str
    input_chars: int
    calls: int = 0
    tokens: int = 0
    elapsed_ms: float = 0.0
    cost_usd: float = 0.0
    executed: bool = True

    def to_dict(self) -> dict:
        return {
            "executed": self.executed,
            "input_chars": self.input_chars,
            "calls": self.calls,
            "tokens": self.tokens,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "cost_usd": round(self.cost_usd, 6),
            "per_wan_tokens": per_wan(self.tokens, self.input_chars),
            "per_wan_ms": per_wan(self.elapsed_ms, self.input_chars),
            "per_wan_cost_usd": per_wan(self.cost_usd, self.input_chars, digits=6),
        }


def pipeline_usage_ledger(stages: list[StageUsage], *, corpus_chars: int) -> dict:
    """汇总 stage 账本：总量 + 每万字。

    复审 P1：**全链路每万字分母必须是原始语料字数（corpus_chars）**——
    同一份一万字语料经过 rewrite/map/消歧/验证/链接等阶段时，若把各阶段
    input_chars 相加会变成「五万字」，系统性低估全链路每万字 Token/耗时/
    成本。阶段行仍保留各自的 input_chars（阶段级每万字各用自身输入量）。
    """
    executed = [s for s in stages if s.executed]
    totals = {
        "input_chars": corpus_chars,
        "calls": sum(s.calls for s in executed),
        "tokens": sum(s.tokens for s in executed),
        "elapsed_ms": round(sum(s.elapsed_ms for s in executed), 1),
        "cost_usd": round(sum(s.cost_usd for s in executed), 6),
    }
    return {
        "stages": {s.stage: s.to_dict() for s in stages},
        "totals": totals,
        "per_wan": {
            "tokens": per_wan(totals["tokens"], corpus_chars),
            "elapsed_ms": per_wan(totals["elapsed_ms"], corpus_chars),
            "cost_usd": per_wan(totals["cost_usd"], corpus_chars, digits=6),
        },
    }
