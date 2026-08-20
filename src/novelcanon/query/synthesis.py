"""证据接地答案合成（阶段 10 §4，docs/implementation/10）。

契约（10 §4）：
- 模型只能接收**过滤后**的上下文（结构化事实 / 检索 chunk，均带证据与
  章节定位）——prompt 由本模块构造，只含调用方传入的 context，绝不访问
  过滤前全文（结构上保证「生成式合成无法访问过滤前上下文」）；
- 区分原文事实（来自 claim/chunk，标 source）与模型推断（标 inferred）；
- 返回章节定位与 evidence；证据不足时明确拒答（cannot_answer）；
- 不使用模型自身记忆补充小说内容（prompt 明示禁止）；
- 记录 query profile、上下文 ID（context hash）与 cutoff 参数。

无模型可用时（client=None）走确定性合成：只把过滤后的事实按模板组织，
不调用模型——仍满足证据接地与上下文隔离。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import Engine, text

from novelcanon.config.hash import stable_config_hash

SYNTHESIS_SCHEMA_VERSION = "synthesis-v2"
DETERMINISTIC_PROMPT_VERSION = "deterministic-v1"


@dataclass(frozen=True)
class ContextItem:
    """一条过滤后上下文（来源事实或原文 chunk，均带定位与证据）。"""

    kind: str  # 'claim' | 'chunk'
    claim_type: str | None = None
    claim_version_id: str | None = None
    chapter_id: str | None = None
    observed_ordinal: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    content: str = ""
    claim_status: str = ""
    confidence: float | None = None
    evidence_stance: str = ""

    def source_label(self) -> str:
        return f"{self.kind}:{self.claim_type or ''}@{self.observed_ordinal}"


@dataclass(frozen=True)
class AnswerSource:
    """答案引用的证据条目（章节定位 + evidence stance）。"""

    claim_version_id: str | None
    chapter_id: str | None
    observed_ordinal: int | None
    char_start: int | None
    char_end: int | None
    stance: str
    kind: str
    label: str


@dataclass(frozen=True)
class AnswerResult:
    """合成答案：正文 + 来源 + 诊断（route/context/cutoff 记录）。"""

    answer: str
    route: str
    query_type: str
    sources: list[AnswerSource] = field(default_factory=list)
    confidence: float = 0.0
    caveats: list[str] = field(default_factory=list)
    context_id: str = ""
    query_profile: str = ""
    profile_id: str = ""
    knowledge_cutoff: int | None = None
    world_at: int | None = None
    synthesized: bool = False  # False = 确定性模板合成
    cannot_answer: bool = False


class _Client(Protocol):
    async def complete(self, prompt: str) -> Any: ...


class SynthesisService:
    """book_id 绑定的答案合成（模型可选，无模型走确定性模板）。"""

    def __init__(
        self,
        engine: Engine,
        book_id: str,
        *,
        client: _Client | None = None,
        profile_id: str = "",
        query_profile: str = "",
    ) -> None:
        self._engine = engine
        self._book_id = book_id
        self._client = client
        self._profile_id = profile_id or (getattr(client, "profile_id", "") if client else "")
        self._query_profile = query_profile or "default"

    @property
    def query_profile(self) -> str:
        return self._query_profile

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def has_client(self) -> bool:
        return self._client is not None

    # ── 对外 ────────────────────────────────────────────────────

    def answer(
        self,
        question: str,
        *,
        route: str,
        query_type: str,
        context: list[ContextItem],
        knowledge_cutoff: int | None = None,
        world_at: int | None = None,
    ) -> AnswerResult:
        """基于过滤后上下文合成答案（route/context/cutoff 全程记录）。"""
        context_id = stable_config_hash(
            {
                "book": self._book_id,
                "route": route,
                "query_type": query_type,
                "items": [
                    {
                        "kind": c.kind,
                        "claim_version_id": c.claim_version_id,
                        "chapter_id": c.chapter_id,
                        "ordinal": c.observed_ordinal,
                        "content": c.content,
                    }
                    for c in context
                ],
                "cutoff": knowledge_cutoff,
                "world": world_at,
            }
        )
        if not context:
            return AnswerResult(
                answer=(
                    "证据不足：过滤后上下文为空，无法回答。"
                    "可尝试放宽 knowledge cutoff 或改用原文检索路线。"
                ),
                route=route,
                query_type=query_type,
                confidence=0.0,
                caveats=["证据不足，明确拒答（10 §4）"],
                context_id=context_id,
                query_profile=self._query_profile,
                profile_id=self._profile_id,
                knowledge_cutoff=knowledge_cutoff,
                world_at=world_at,
                cannot_answer=True,
            )
        if self._client is not None:
            return self._llm_answer(
                question,
                route=route,
                query_type=query_type,
                context=context,
                context_id=context_id,
                knowledge_cutoff=knowledge_cutoff,
                world_at=world_at,
            )
        return self._deterministic_answer(
            question,
            route=route,
            query_type=query_type,
            context=context,
            context_id=context_id,
            knowledge_cutoff=knowledge_cutoff,
            world_at=world_at,
        )

    # ── 模型路径（prompt 只含过滤后上下文）──────────────────────

    def _llm_answer(
        self,
        question: str,
        *,
        route: str,
        query_type: str,
        context: list[ContextItem],
        context_id: str,
        knowledge_cutoff: int | None,
        world_at: int | None,
    ) -> AnswerResult:
        prompt = self._build_prompt(
            question,
            route=route,
            query_type=query_type,
            context=context,
            profile_id=self._profile_id,
            prompt_version=SYNTHESIS_SCHEMA_VERSION,
            knowledge_cutoff=knowledge_cutoff,
            world_at=world_at,
        )
        import asyncio

        assert self._client is not None  # _llm_answer 仅在 has_client 时调用
        result = asyncio.run(self._client.complete(prompt))
        try:
            payload = json.loads(result.raw_text)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        answer = str(payload.get("answer") or "").strip()
        if not answer:
            answer = "证据不足：无法合成答案（模型未给出有效回答）。"
            caveats = ["模型输出不可解析，明确拒答"]
            cannot_answer = True
        else:
            caveats = [
                str(c) for c in (payload.get("caveats") or [])
            ] if isinstance(payload.get("caveats"), list) else []
            caveats.append("区分：claim 内容为原文事实，模型推断另作标注")
            cannot_answer = False
        try:
            confidence = float(payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        return AnswerResult(
            answer=answer,
            route=route,
            query_type=query_type,
            sources=self._sources_from(context),
            confidence=confidence,
            caveats=caveats,
            context_id=context_id,
            query_profile=self._query_profile,
            profile_id=self._profile_id,
            knowledge_cutoff=knowledge_cutoff,
            world_at=world_at,
            synthesized=True,
            cannot_answer=cannot_answer,
        )

    def _build_prompt(
        self,
        question: str,
        *,
        route: str,
        query_type: str,
        context: list[ContextItem],
        profile_id: str,
        prompt_version: str,
        knowledge_cutoff: int | None,
        world_at: int | None,
    ) -> str:
        """构造合成 prompt：只含过滤后上下文，明示不得用模型记忆补充。"""
        lines = [
            "你是 NovelCanon 查询合成器。只依据下面给出的「过滤后上下文」回答，",
            "不得使用模型自身记忆补充小说内容，不得编造证据。",
            f"问题（route={route} / type={query_type}）：{question}",
            f"knowledge_cutoff={knowledge_cutoff} / world_at={world_at}",
            "上下文：",
        ]
        for i, c in enumerate(context, start=1):
            loc = (
                f"章{c.observed_ordinal}"
                + (f"[{c.char_start}:{c.char_end}]" if c.char_start is not None else "")
                if c.observed_ordinal is not None
                else ""
            )
            stance = f" 证据立场={c.evidence_stance}" if c.evidence_stance else ""
            lines.append(
                f"{i}. [{c.source_label()} @ {loc}{stance}] {c.content}"
            )
        lines.append(
            "输出 JSON：{\"answer\": 答案正文, \"confidence\": 0-1,"
            " \"caveats\": [不确定/推断项列表]}。"
            "引用上下文编号；上下文没有的信息回答\"证据不足\"。"
        )
        return "\n".join(lines)

    # ── 确定性路径（无模型：模板合成过滤后事实）──────────────────

    def _deterministic_answer(
        self,
        question: str,
        *,
        route: str,
        query_type: str,
        context: list[ContextItem],
        context_id: str,
        knowledge_cutoff: int | None,
        world_at: int | None,
    ) -> AnswerResult:
        facts = [c for c in context if c.kind == "claim"]
        chunks = [c for c in context if c.kind == "chunk"]
        summaries = [c for c in context if c.kind == "summary"]
        parts: list[str] = []
        if summaries:
            parts.append("分层摘要（卷/全书）：")
            for c in summaries:
                parts.append(f"- {c.content}")
        if facts:
            parts.append("原文事实：")
            for c in facts:
                parts.append(
                    f"- [{c.claim_type}]（章{c.observed_ordinal}，"
                    f"状态={c.claim_status or 'supported'}）：{c.content}"
                )
        if chunks:
            parts.append("原文片段：")
            for c in chunks:
                parts.append(f"- 章{c.observed_ordinal}：{c.content}")
        if not parts:
            answer = "证据不足：过滤后上下文为空，无法回答。"
            cannot_answer = True
        else:
            answer = "\n".join(parts)
            answer += (
                "\n\n（确定性合成：仅转述过滤后的结构化事实/原文片段，"
                "不含模型推断。）"
            )
            cannot_answer = False
        return AnswerResult(
            answer=answer,
            route=route,
            query_type=query_type,
            sources=self._sources_from(context),
            confidence=0.5 if not cannot_answer else 0.0,
            caveats=["确定性合成：未做模型推断"] if not cannot_answer else [],
            context_id=context_id,
            query_profile=self._query_profile,
            profile_id=self._profile_id,
            knowledge_cutoff=knowledge_cutoff,
            world_at=world_at,
            synthesized=False,
            cannot_answer=cannot_answer,
        )

    # ── 证据条目整理 ────────────────────────────────────────────

    @staticmethod
    def _sources_from(context: list[ContextItem]) -> list[AnswerSource]:
        return [
            AnswerSource(
                claim_version_id=c.claim_version_id,
                chapter_id=c.chapter_id,
                observed_ordinal=c.observed_ordinal,
                char_start=c.char_start,
                char_end=c.char_end,
                stance=c.evidence_stance or c.claim_status or "",
                kind=c.kind,
                label=c.source_label(),
            )
            for c in context
        ]

    # ── 章节定位辅助（10 §4「返回章节定位」）────────────────────

    def chapter_title(self, chapter_id: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT title FROM chapters WHERE chapter_id = :c AND book_id = :b"
                ),
                {"c": chapter_id, "b": self._book_id},
            ).fetchone()
        return row[0] if row else None
