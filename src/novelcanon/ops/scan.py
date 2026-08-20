"""持续回归扫描（阶段 11 P5，docs/implementation/11 §P5）。

- scan_cutoff_leakage：对黄金问题 × 多档 cutoff 跑结构化/混合查询，
  断言所有来源章节 ordinal ≤ cutoff——「cutoff 泄露回归为零」验证项；
- scan_integrity：外键 + 证据复现 + claims 幂等完整性（复用 backup 模块）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine

from novelcanon.query import QueryExecutor
from novelcanon.retrieval.factory import NoActiveIndexError, backend_for_active_index
from novelcanon.storage.backup import IntegrityReport, verify_integrity


@dataclass(frozen=True)
class LeakageFinding:
    question: str
    cutoff: int
    route: str
    leaked_ordinals: list[int]
    source_ordinals: list[int]

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "cutoff": self.cutoff,
            "route": self.route,
            "leaked_ordinals": self.leaked_ordinals,
            "source_ordinals": self.source_ordinals,
        }


@dataclass(frozen=True)
class CutoffScanResult:
    findings: list[LeakageFinding] = field(default_factory=list)
    checks: int = 0

    @property
    def leaked(self) -> bool:
        return bool(self.findings)

    def as_dict(self) -> dict:
        return {
            "checks": self.checks,
            "leaked": self.leaked,
            "findings": [f.as_dict() for f in self.findings],
        }


def scan_cutoff_leakage(
    engine: Engine,
    book_id: str,
    questions: list[str],
    cutoffs: list[int],
) -> CutoffScanResult:
    """逐问题 × 逐 cutoff：来源章节不得超过 cutoff（结构化 + 混合）。

    运行时后端按 active index 统一创建（复审 D P1）：真实索引必须用真实
    adapter，否则 profile mismatch；**仅无 active 索引**（NoActiveIndexError）
    时结构化查询 fake 兜底——配置校验错误原样上报（复审 D P2）。
    """
    try:
        embedder, vector_store = backend_for_active_index(engine, book_id)
    except NoActiveIndexError:
        from novelcanon.retrieval.vectorstore import BruteForceVectorStore, FakeEmbedder

        embedder, vector_store = FakeEmbedder(dimension=8), BruteForceVectorStore(dimension=8)
    executor = QueryExecutor(
        engine,
        book_id,
        embedder=embedder,
        vector_store=vector_store,
        use_cache=False,
    )
    try:
        findings: list[LeakageFinding] = []
        checks = 0
        for q in questions:
            for cutoff in cutoffs:
                checks += 1
                answer = executor.ask(q, knowledge_cutoff=cutoff).answer
                ordinals = sorted(
                    {
                        s["observed_ordinal"]
                        for s in (answer.get("sources") or [])
                        if s.get("observed_ordinal") is not None
                    }
                )
                leaked = [o for o in ordinals if o > cutoff]
                if leaked:
                    findings.append(
                        LeakageFinding(
                            question=q,
                            cutoff=cutoff,
                            route=answer.get("route", ""),
                            leaked_ordinals=leaked,
                            source_ordinals=ordinals,
                        )
                    )
    finally:
        closer = getattr(embedder, "close", None)
        if closer is not None:
            closer()
    return CutoffScanResult(findings=findings, checks=checks)


def scan_integrity(engine: Engine, *, evidence_sample: int = 50) -> IntegrityReport:
    """完整性扫描（外键 + 证据复现 + 幂等）。"""
    return verify_integrity(engine, evidence_sample=evidence_sample)
