"""评测指标计算（阶段 11 P1，docs/implementation/11 §P1）。

所有指标均为确定性函数：输入黄金集 + 系统输出，输出指标字典。
阈值以《定版方案》§14.2 为准，正式阈值在查看评测结果前写入测试配置。
"""

from __future__ import annotations

from dataclasses import dataclass

from novelcanon.eval.golden import GoldenQA


@dataclass(frozen=True)
class Metrics:
    """precision / recall / F1（全零分母时记 0 并标注分母为空）。"""

    precision: float
    recall: float
    f1: float
    golden_count: int
    predicted_count: int

    @staticmethod
    def of(golden: set, predicted: set) -> Metrics:
        if not golden and not predicted:
            return Metrics(0.0, 0.0, 0.0, 0, 0)
        tp = len(golden & predicted)
        precision = tp / len(predicted) if predicted else 0.0
        recall = tp / len(golden) if golden else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        return Metrics(precision, recall, f1, len(golden), len(predicted))

    def as_dict(self) -> dict:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "golden_count": self.golden_count,
            "predicted_count": self.predicted_count,
        }


def entity_merge_metrics(golden: list[tuple[str, str]], predicted: list[tuple[str, str]]) -> dict:
    """实体合并 F1：合并对（surface_a, surface_b，无序）的一致性。

    黄金来自 GoldenEntityMerge.surfaces 的两两组合；predicted 来自
    entity_resolutions 的同一 canonical 表面名两两组合。
    """

    def pairs(items: list[tuple[str, str]]) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for a, b in items:
            if a != b:
                key: tuple[str, str] = (min(a, b), max(a, b))
                out.add(key)
        return out

    return Metrics.of(pairs(golden), pairs(predicted)).as_dict()


def fact_metrics(golden: list[str], predicted: list[str]) -> dict:
    """事实 precision/recall：黄金事实描述 vs 预测事实描述（规范化后比较）。"""
    norm = lambda x: " ".join(x.split())  # noqa: E731 —— 规范化空白
    return Metrics.of({norm(g) for g in golden}, {norm(p) for p in predicted}).as_dict()


def evidence_hash_reproduction(golden_spans: list[str], predicted_hashes: set[str]) -> dict:
    """direct evidence hash 复现率：黄金原文 span 的 hash 是否被复现。

    黄金 span 用与系统相同的 sha256 规范化哈希；predicted_hashes 来自
    claim_evidence.span_hash。
    """
    from novelcanon.ingestion.normalize import sha256

    golden_hashes = {sha256(g) for g in golden_spans}
    if not golden_hashes:
        return {"reproduction_rate": 0.0, "golden_count": 0, "reproduced": 0}
    reproduced = len(golden_hashes & predicted_hashes)
    return {
        "reproduction_rate": round(reproduced / len(golden_hashes), 4),
        "golden_count": len(golden_hashes),
        "reproduced": reproduced,
    }


def qa_chapter_accuracy(answers: list[dict], golden_qas: list[GoldenQA]) -> dict:
    """QA 引用章节正确率：回答 sources 的章节集合命中黄金期望章节。

    每个来源携带 observed_ordinal；命中 = 预测集合与黄金集合有交集。
    分母恒为全部黄金问题：缺失的回答（answers 少于 golden_qas）记为
    未命中（answered=False），不得因少返回答案而抬高准确率（P0）。
    """
    per_question: list[dict] = []
    by_index = {i: a for i, a in enumerate(answers)}
    for idx, qa in enumerate(golden_qas):
        answer = by_index.get(idx)
        answered = answer is not None
        pred: set[int] = set()
        if answered:
            assert answer is not None  # mypy 收窄（answered 已判非 None）
            pred = {
                s["observed_ordinal"]
                for s in (answer.get("sources") or [])
                if s.get("observed_ordinal") is not None
            }
        gold = set(qa.chapter_ordinals)
        per_question.append(
            {
                "question": qa.question,
                "predicted": sorted(pred),
                "golden": sorted(gold),
                "hit": bool(pred & gold),
                "answered": answered,
            }
        )
    total = len(per_question)
    return {
        "accuracy": round(sum(1 for p in per_question if p["hit"]) / total, 4) if total else 0.0,
        "answered": sum(1 for p in per_question if p["answered"]),
        "per_question": per_question,
    }


def causal_edge_precision(golden: list[tuple[str, str]], predicted: list[tuple[str, str]]) -> dict:
    """因果边 precision：系统 supported 边与黄金边的匹配率。

    边以 (源事件摘要, 目标事件摘要) 表示；precision 优先（11 已知边界：
    因果 precision 为阶段 11 P1 正式指标）。
    """
    norm = lambda s: " ".join(s.split())  # noqa: E731
    g = {(norm(a), norm(b)) for a, b in golden}
    p = {(norm(a), norm(b)) for a, b in predicted}
    if not p:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "predicted_count": 0}
    return Metrics.of(g, p).as_dict()
