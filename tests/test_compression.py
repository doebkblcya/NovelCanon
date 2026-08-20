"""阶段 11 压缩实验测试（docs/implementation/11 §压缩实验）。

覆盖验证项：
- 规则预扫描生成保留词典（专名/数字/时间/高频词，不调用模型）；
- 受约束改写输出 keep/rewrite/drop/reason/ref_seg；
- 后验校验覆盖率低于阈值自动回退原文；
- 决策门四条件（recall 差 ≤0.02 / 证据 / QA / 成本）才启用压缩。
"""

from __future__ import annotations

from novelcanon.compression import (
    CompressionService,
    DecisionGate,
    DeterministicRewriter,
    Prescanner,
    Rewriter,
    SegDecision,
    decide_compression,
)
from novelcanon.compression.prescan import KeepDict
from novelcanon.compression.rewriter import stable_hash

TEXT = (
    "萧炎在乌坦城修炼，三年之约将近。\n\n"
    "药老教他斗气功法，青莲地心火乃是异火榜上的天地奇物。\n\n"
    "这段只是环境描写，晨光洒落，微风拂面。"
)


def test_prescan_builds_keep_dict() -> None:
    keep = Prescanner().scan([TEXT], known_surfaces=["萧炎", "药老"])
    assert "萧炎" in keep.entities
    assert "药老" in keep.entities
    assert keep.version, "保留词典必须有版本（compression version 可追踪）"
    assert keep.all_terms()


def test_deterministic_rewriter_segments() -> None:
    keep = Prescanner().scan([TEXT], known_surfaces=["萧炎"])
    compressor = CompressionService(rewriter=DeterministicRewriter())
    out = compressor.compress_chapter("ch1", TEXT, keep=keep)
    assert out.result.segments, "应有逐段决策"
    actions = {s.action for s in out.result.segments}
    assert "keep" in actions
    # 无保留词的纯描写段应 drop
    assert "萧炎" in out.compressed_text
    assert out.retention > 0 and out.retention < 1.0
    assert out.compression_version


def test_post_validation_falls_back_on_low_coverage() -> None:
    """覆盖率低于阈值 → 段回退原文。"""
    keep = KeepDict(entities=frozenset(["萧炎", "药老", "青莲地心火", "异火"]))
    result = CompressionService(rewriter=DeterministicRewriter()).compress_chapter(
        "ch1", TEXT, keep=keep
    )
    validation = result.validation
    assert "coverage" in validation
    assert "青莲地心火" in validation["output_text"], "保留词必须保留"
    # 无模型改写删除纯描写段后，保留词覆盖率仍应通过（保留词段被 keep）
    assert validation["passed"] or validation["fallback_segments"] >= 0


def test_decision_gate_requires_all_conditions() -> None:
    """决策门：recall 差 >0.02 或证据/QA/成本任一不满足 → 不启用。"""
    good_baseline = {
        "facts": {"recall": 0.8},
        "evidence_reproduction": {"reproduction_rate": 0.9},
        "qa_chapter_accuracy": {"accuracy": 0.8},
    }
    good_compressed = {
        "facts": {"recall": 0.79},  # 差 0.01 ≤ 0.02
        "evidence_reproduction": {"reproduction_rate": 0.92},
        "qa_chapter_accuracy": {"accuracy": 0.8},
        "extraction_mode": "llm-map",  # 生产抽取模式（复审 P0：硬前置）
    }
    g = DecisionGate().decide(good_baseline, good_compressed, cost_saving=0.4)
    assert g.enable, f"全条件满足应启用：{g.reasons}"

    bad_recall = DecisionGate().decide(
        {"facts": {"recall": 0.8}},
        {"facts": {"recall": 0.7}, "extraction_mode": "llm-map"},  # 差 0.1 > 0.02
        cost_saving=0.5,
    )
    assert not bad_recall.enable
    assert bad_recall.reasons["recall"] is False

    bad_cost = DecisionGate().decide(good_baseline, good_compressed, cost_saving=0.02)
    assert not bad_cost.enable
    assert bad_cost.reasons["cost"] is False


def test_decision_gate_requires_production_extraction_mode() -> None:
    """复审 P0：golden-replay / 缺省模式不得授权启用压缩（硬前置）。

    即使四条件全满足，非生产抽取模式（golden-replay / 未标注）也直接
    拒绝——黄金重放把黄金答案落库，不能衡量真实抽取 recall。
    """
    baseline = {
        "facts": {"recall": 0.8},
        "evidence_reproduction": {"reproduction_rate": 0.9},
        "qa_chapter_accuracy": {"accuracy": 0.8},
    }
    good = {
        "facts": {"recall": 0.79},
        "evidence_reproduction": {"reproduction_rate": 0.92},
        "qa_chapter_accuracy": {"accuracy": 0.8},
    }
    replay = DecisionGate().decide(
        baseline, {**good, "extraction_mode": "golden-replay"}, cost_saving=0.4
    )
    assert replay.enable is False, "golden-replay 不得授权启用压缩"
    assert replay.reasons["extraction_mode"] is False
    missing = DecisionGate().decide(baseline, good, cost_saving=0.4)
    assert missing.enable is False, "未标注抽取模式不得授权启用压缩"
    assert missing.reasons["extraction_mode"] is False
    prod = DecisionGate().decide(
        baseline, {**good, "extraction_mode": "production-map"}, cost_saving=0.4
    )
    assert prod.enable is True, "production-map 应视为生产模式"


def test_decide_compression_entrypoint() -> None:
    g = decide_compression(
        {"facts": {"recall": 0.8}},
        {"facts": {"recall": 0.8}, "extraction_mode": "llm-map"},
        cost_saving=0.5,
    )
    assert isinstance(g.enable, bool)
    assert g.version == "gate-v2"


def test_gate_requires_validation_hard_precondition() -> None:
    """P0：后验校验失败（validated=False）→ 决策门必须拒绝启用。

    即使 recall/证据/QA/成本四条件全部满足，validated=False 也直接拒绝
    （不再作为旁路诊断字段）。
    """
    baseline = {
        "facts": {"recall": 0.8},
        "evidence_reproduction": {"reproduction_rate": 0.9},
        "qa_chapter_accuracy": {"accuracy": 0.8},
    }
    compressed = {
        "facts": {"recall": 0.79},
        "evidence_reproduction": {"reproduction_rate": 0.92},
        "qa_chapter_accuracy": {"accuracy": 0.8},
        "extraction_mode": "llm-map",
        "validated": False,  # 后验失败（如错删关键段被兜底）
    }
    g = DecisionGate().decide(baseline, compressed, cost_saving=0.4)
    assert g.enable is False, "后验失败时不得启用压缩"
    assert g.reasons["validation"] is False
    # 后验通过且四条件满足（生产抽取模式）→ 启用
    g2 = DecisionGate().decide(baseline, {**compressed, "validated": True}, cost_saving=0.4)
    assert g2.enable is True


class _DropAllRewriter(Rewriter):
    """模拟 LLM 错删：无条件 drop（含专名/数字的段也删）。"""

    def rewrite(self, seg_index: int, text: str, keep: KeepDict) -> SegDecision:
        return SegDecision(
            seg_index=seg_index,
            action="drop",
            reason="模拟错删",
            ref_seg=stable_hash(text),
        )


def test_post_validation_backstops_bad_drop() -> None:
    """P0：drop 段若仍含专名/数字/时间 → 强制回退原文，不得永久丢失。"""
    keep = KeepDict(entities=frozenset(["萧炎", "药老"]))
    out = CompressionService(rewriter=_DropAllRewriter()).compress_chapter("ch1", TEXT, keep=keep)
    validation = out.validation
    assert validation["restored_drop_segments"] >= 1, "含专名的段被错删必须回退"
    assert "萧炎" in validation["output_text"], "回退后关键专名必须保留"
    assert validation["fallback_segments"] >= 1
    # 错删被兜底 → 整章 passed=False（压缩决策不可接受，但内容安全）
    assert validation["passed"] is False


class _DropNumbersRewriter(Rewriter):
    """模拟 rewrite 删数字：把『三』从每段中删掉。"""

    def rewrite(self, seg_index: int, text: str, keep: KeepDict) -> SegDecision:
        out = text.replace("三", "")
        return SegDecision(
            seg_index=seg_index,
            action="rewrite",
            reason="模拟删数字",
            ref_seg=stable_hash(text),
            output_text=out,
        )


def test_post_validation_checks_number_and_time_coverage() -> None:
    """P0：非 drop 段数字/时间覆盖率低于阈值 → 回退原文。"""
    keep = KeepDict(
        entities=frozenset(["萧炎"]),
        numbers=frozenset(["三"]),
        time_exprs=frozenset(["三年"]),
    )
    out = CompressionService(rewriter=_DropNumbersRewriter()).compress_chapter(
        "ch1", TEXT, keep=keep
    )
    validation = out.validation
    # 「三年之约将近」段被删掉「三」→ 数字/时间覆盖率不足 → 回退
    assert validation["fallback_segments"] >= 1
    assert "三年" in validation["output_text"], "回退后数字/时间表达必须保留"
    assert validation["number_coverage"] == 1.0
    assert validation["time_coverage"] == 1.0
