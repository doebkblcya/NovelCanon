"""分层 Reduce（阶段 10 §7，docs/implementation/10）。

章节结构化记忆 → 卷摘要 → 全书摘要。每个摘要保存：
- 输入 claim 版本集合（input_claim_versions，JSON）；
- generation/profile/prompt 版本（generation_profile_id/prompt_version/
  schema_version）；
- content hash（摘要内容 hash，用于依赖链与版本区分）；
- max observed ordinal（输入章节记忆的最大披露章节）；
- 依赖的下级摘要版本（depends_on_summaries）。

输入事实（claim 版本集合）变化 → 摘要 content 变化 → 新 summary_id
（新版本行），旧行标记 stale；全书摘要依赖的卷摘要失效时按依赖图
标记失效并重建（10 §7「输入事实变化时按依赖图标记失效并重建」）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from novelcanon.config.hash import stable_config_hash
from novelcanon.storage.repository import now_iso
from novelcanon.summaries.volumes import GroupingResult, VolumeGrouper

REDUCER_SCHEMA_VERSION = "reducer-v1"
DETERMINISTIC_PROMPT_VERSION = "deterministic-v1"


@dataclass(frozen=True)
class ChapterMemory:
    """章节结构化记忆：ordinal + claim 摘要（输入摘要的原子单位）。"""

    ordinal: int
    chapter_id: str
    title: str | None
    claims: list[dict] = field(default_factory=list)

    def claim_version_ids(self) -> list[str]:
        return sorted({c["claim_version_id"] for c in self.claims})


@dataclass(frozen=True)
class SummaryResult:
    book_id: str
    grouping: GroupingResult
    chapter_memories: int
    volume_summaries: list[dict]
    book_summary: dict | None
    stale: int = 0
    rebuilt: int = 0
    reused: int = 0


class Summarizer:
    """摘要生成器抽象：输入章节记忆，输出摘要文本（JSON 或纯文本）。"""

    def summarize(self, memories: list[ChapterMemory], *, title: str) -> str:
        raise NotImplementedError


class DeterministicSummarizer(Summarizer):
    """提取式摘要（无模型）：按 ordinal 组织关键 claim，不调用模型。"""

    def summarize(self, memories: list[ChapterMemory], *, title: str) -> str:
        lines = [f"《{title}》结构化摘要（确定性提取，无模型推断）："]
        for m in sorted(memories, key=lambda x: x.ordinal):
            events = [c for c in m.claims if c["claim_type"] == "event"]
            others = [c for c in m.claims if c["claim_type"] != "event"]
            parts: list[str] = []
            for ev in events:
                parts.append(
                    f"[事件:{ev['payload'].get('event_type', '')}] "
                    f"{ev['payload'].get('summary', '')}"
                )
            for o in others[:8]:
                payload = o["payload"]
                if o["claim_type"] == "relation":
                    parts.append(
                        f"关系:{payload.get('from_entity_id', '')}"
                        f"—[{payload.get('relation_type', '')}]→"
                        f"{payload.get('to_entity_id', '')}"
                    )
                elif o["claim_type"] == "state":
                    parts.append(
                        f"状态:{payload.get('field', '')}={payload.get('value', '')}"
                    )
                elif o["claim_type"] == "org":
                    parts.append(
                        f"势力:{payload.get('member_entity_id', '')}"
                        f"→{payload.get('org_entity_id', '')}"
                        f"({payload.get('role', '')})"
                    )
            if parts:
                lines.append(f"第{m.ordinal}章{m.title or ''}：{'；'.join(parts)}")
        return "\n".join(lines)


class LLMSummarizer(Summarizer):
    """模型摘要：prompt 只含章节记忆（结构化 JSON），输出 JSON。"""

    def __init__(
        self,
        client,
        *,
        profile_id: str = "",
        prompt_version: str = "llm-summary-v1",
    ) -> None:
        self._client = client
        self.profile_id = profile_id or getattr(client, "profile_id", "")
        self.prompt_version = prompt_version

    def summarize(self, memories: list[ChapterMemory], *, title: str) -> str:
        import asyncio

        prompt = self._build_prompt(memories, title=title)
        result = asyncio.run(self._client.complete(prompt))
        raw = result.raw_text.strip()
        return raw or "（模型未返回有效摘要）"

    def _build_prompt(self, memories: list[ChapterMemory], *, title: str) -> str:
        lines = [
            "你是 NovelCanon 分层摘要器。只依据给定的章节结构化记忆生成摘要，",
            "不得使用模型自身记忆补充小说内容。",
            f"卷/书名：{title}",
            "章节记忆（JSON）：",
            json.dumps(
                [
                    {
                        "ordinal": m.ordinal,
                        "title": m.title,
                        "claims": m.claims,
                    }
                    for m in sorted(memories, key=lambda x: x.ordinal)
                ],
                ensure_ascii=False,
            ),
            '输出 JSON：{"summary": 摘要正文, "key_events": [关键事件], '
            '"key_entities": [关键实体]}。',
        ]
        return "\n".join(lines)


class HierarchicalReducer:
    """book_id 绑定的分层 Reduce（章节 → 卷 → 全书）。"""

    def __init__(
        self,
        engine: Engine,
        book_id: str,
        *,
        summarizer: Summarizer | None = None,
        chapters_per_volume: int = 50,
        schema_version: str = REDUCER_SCHEMA_VERSION,
    ) -> None:
        self._engine = engine
        self._book_id = book_id
        self._summarizer = summarizer or DeterministicSummarizer()
        self._grouper = VolumeGrouper(
            engine, book_id, chapters_per_volume=chapters_per_volume
        )
        self._schema_version = schema_version

    # ── 对外 ────────────────────────────────────────────────────

    def reduce(self, *, cutoff: int | None = None) -> SummaryResult:
        """分层 Reduce：分组 → 卷摘要 → 全书摘要（幂等 + 失效重建）。"""
        grouping = self._grouper.group()
        memories = self._chapter_memories(cutoff)

        volume_summaries: list[dict] = []
        rebuilt = 0
        reused = 0
        for vol in grouping.volumes:
            vol_memories = [
                m
                for m in memories
                if (vol.start_ordinal == 0 or vol.start_ordinal <= m.ordinal)
                and (vol.end_ordinal == 0 or m.ordinal <= vol.end_ordinal)
            ]
            if not vol_memories:
                continue
            out = self._upsert_summary(
                level="volume",
                volume_id=vol.volume_id,
                grouping_version=grouping.grouping_version,
                title=vol.title,
                memories=vol_memories,
                depends_on=[],
            )
            volume_summaries.append(out)
            rebuilt += 1 if out["rebuilt"] else 0
            reused += 1 if out["reused"] else 0

        book_summary: dict | None = None
        if volume_summaries:
            book_out = self._upsert_summary(
                level="book",
                volume_id=None,
                grouping_version=None,
                title=self._book_title(),
                memories=memories,
                depends_on=[s["summary_id"] for s in volume_summaries],
            )
            book_summary = book_out
            rebuilt += 1 if book_out["rebuilt"] else 0
            reused += 1 if book_out["reused"] else 0

        stale = self._stale_count()
        return SummaryResult(
            book_id=self._book_id,
            grouping=grouping,
            chapter_memories=len(memories),
            volume_summaries=volume_summaries,
            book_summary=book_summary,
            stale=stale,
            rebuilt=rebuilt,
            reused=reused,
        )

    # ── 章节记忆 ────────────────────────────────────────────────

    def _chapter_memories(self, cutoff: int | None) -> list[ChapterMemory]:
        cutoff_sql = ""
        params: dict[str, object] = {"book": self._book_id}
        if cutoff is not None:
            cutoff_sql = "AND c.observed_ordinal <= :cutoff"
            params["cutoff"] = cutoff
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT q.claim_version_id, q.claim_type, q.observed_ordinal,"
                        " q.observed_chapter_id, q.payload, q.confidence FROM ("
                        "  SELECT c.claim_version_id, c.claim_type, c.observed_ordinal,"
                        "         c.observed_chapter_id, c.confidence,"
                        "         c.operation, c.claim_status,"
                        "         COALESCE(s.payload, r.payload, e.payload, o.payload,"
                        "                  td.payload, '{}') AS payload,"
                        "         ROW_NUMBER() OVER (PARTITION BY c.fact_id"
                        "           ORDER BY c._rowid DESC) rn"
                        "  FROM v_active_claims c"
                        "  LEFT JOIN ("
                        "    SELECT claim_version_id,"
                        "      json_object('field', field, 'value', value,"
                        "                 'raw_value', raw_value) AS payload"
                        "    FROM state_claims) s"
                        "    ON s.claim_version_id = c.claim_version_id"
                        "  LEFT JOIN ("
                        "    SELECT claim_version_id,"
                        "      json_object('from_entity_id', from_entity_id,"
                        "                 'to_entity_id', to_entity_id,"
                        "                 'relation_type', relation_type) AS payload"
                        "    FROM relation_claims) r"
                        "    ON r.claim_version_id = c.claim_version_id"
                        "  LEFT JOIN ("
                        "    SELECT claim_version_id,"
                        "      json_object('event_type', event_type, 'summary', summary)"
                        "      AS payload"
                        "    FROM event_claims) e"
                        "    ON e.claim_version_id = c.claim_version_id"
                        "  LEFT JOIN ("
                        "    SELECT claim_version_id,"
                        "      json_object('org_entity_id', org_entity_id,"
                        "                 'member_entity_id', member_entity_id,"
                        "                 'role', role, 'action', action) AS payload"
                        "    FROM org_claims) o"
                        "    ON o.claim_version_id = c.claim_version_id"
                        "  LEFT JOIN ("
                        "    SELECT claim_version_id,"
                        "      json_object('term_id', term_id, 'definition', definition)"
                        "      AS payload"
                        "    FROM term_definition_claims) td"
                        "    ON td.claim_version_id = c.claim_version_id"
                        "  WHERE c.book_id = :book"
                        f"  {cutoff_sql}"
                        ") q"
                        " WHERE q.rn = 1 AND q.operation != 'retract'"
                        "   AND q.claim_status = 'supported'"
                        " ORDER BY q.observed_ordinal"
                    ),
                    params,
                )
                .mappings()
                .fetchall()
            )
        # 章节标题
        with self._engine.connect() as conn:
            titles = {
                r[0]: r[1]
                for r in conn.execute(
                    text(
                        "SELECT chapter_id, title FROM chapters"
                        " WHERE book_id = :b"
                    ),
                    {"b": self._book_id},
                ).fetchall()
            }
        by_chapter: dict[int, ChapterMemory] = {}
        for r in rows:
            ordinal = r["observed_ordinal"]
            if ordinal is None:
                continue
            if ordinal not in by_chapter:
                by_chapter[ordinal] = ChapterMemory(
                    ordinal=ordinal,
                    chapter_id=r["observed_chapter_id"] or "",
                    title=titles.get(r["observed_chapter_id"]),
                )
            try:
                payload = json.loads(r["payload"]) if r["payload"] else {}
            except (json.JSONDecodeError, TypeError):
                payload = {}
            by_chapter[ordinal].claims.append(
                {
                    "claim_version_id": r["claim_version_id"],
                    "claim_type": r["claim_type"],
                    "payload": payload,
                    "confidence": r["confidence"],
                }
            )
        return [by_chapter[k] for k in sorted(by_chapter)]

    # ── 摘要落库（幂等 + 失效重建）──────────────────────────────

    def _upsert_summary(
        self,
        *,
        level: str,
        volume_id: str | None,
        grouping_version: str | None,
        title: str,
        memories: list[ChapterMemory],
        depends_on: list[str],
    ) -> dict:
        claim_versions = sorted(
            {c for m in memories for c in m.claim_version_ids()}
        )
        max_ordinal = max((m.ordinal for m in memories), default=0)
        # 输入标识：claim 版本集合 + 依赖 + 标题 + prompt 版本（10 §7 输入集合）
        input_claim_versions_json = json.dumps(claim_versions, ensure_ascii=False)
        depends_json = json.dumps(sorted(depends_on), ensure_ascii=False)
        prompt_version = self._prompt_version()
        scope = volume_id if volume_id is not None else level

        # 幂等：同 scope 已有 valid 且输入集合一致 → 复用（不重跑）
        existing = self._valid_summary(level, volume_id)
        if (
            existing is not None
            and existing["input_claim_versions"] == input_claim_versions_json
            and existing["depends_on_summaries"] == depends_json
            and existing["prompt_version"] == prompt_version
        ):
            return self._row_dict(existing, rebuilt=False, reused=True)

        content = self._summarizer.summarize(
            memories, title=title or f"{level}摘要"
        )
        content_hash = stable_config_hash({"content": content})
        summary_id = stable_config_hash(
            {
                "book": self._book_id,
                "level": level,
                "scope": scope,
                "content_hash": content_hash,
            }
        )
        with self._engine.begin() as conn:
            # 同 id 已存在（历史同内容）→ 恢复 valid 并更新输入信息
            prev = conn.execute(
                text(
                    "SELECT summary_id FROM summary_artifacts WHERE summary_id = :id"
                ),
                {"id": summary_id},
            ).fetchone()
            if prev is not None:
                conn.execute(
                    text(
                        "UPDATE summary_artifacts SET status = 'valid',"
                        " input_claim_versions = :inp, depends_on_summaries = :dep,"
                        " prompt_version = :pv, grouping_version = :gv, title = :title,"
                        " created_at = :ts"
                        " WHERE summary_id = :id"
                    ),
                    {
                        "inp": input_claim_versions_json,
                        "dep": depends_json,
                        "pv": prompt_version,
                        "gv": grouping_version,
                        "title": title,
                        "id": summary_id,
                        "ts": now_iso(),
                    },
                )
                rebuilt = False
            else:
                conn.execute(
                    text(
                        "INSERT INTO summary_artifacts (summary_id, book_id, level,"
                        " volume_id, chapter_id, grouping_version, title, content,"
                        " input_claim_versions, depends_on_summaries,"
                        " generation_profile_id, prompt_version, schema_version,"
                        " content_hash, max_observed_ordinal, status, created_at)"
                        " VALUES (:id, :b, :level, :vid, NULL, :gv, :title, :content,"
                        " :inp, :dep, :prof, :pv, :sv, :chash, :maxord, 'valid', :ts)"
                    ),
                    {
                        "id": summary_id,
                        "b": self._book_id,
                        "level": level,
                        "vid": volume_id,
                        "gv": grouping_version,
                        "title": title,
                        "content": content,
                        "inp": input_claim_versions_json,
                        "dep": depends_json,
                        "prof": self._profile_id(),
                        "pv": prompt_version,
                        "sv": self._schema_version,
                        "chash": content_hash,
                        "maxord": max_ordinal,
                        "ts": now_iso(),
                    },
                )
                rebuilt = True
            # 同 scope 的其它 valid 摘要标 stale（输入变化 → 新版本）
            conn.execute(
                text(
                    "UPDATE summary_artifacts SET status = 'stale'"
                    " WHERE book_id = :b AND level = :level"
                    " AND summary_id != :id AND status = 'valid'"
                    " AND (:vid IS NULL AND volume_id IS NULL"
                    "      OR volume_id = :vid)"
                ),
                {
                    "b": self._book_id,
                    "level": level,
                    "id": summary_id,
                    "vid": volume_id,
                },
            )
        row = self._fetch_summary(summary_id)
        return self._row_dict(row, rebuilt=rebuilt, reused=False)

    def _valid_summary(self, level: str, volume_id: str | None) -> dict | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT * FROM summary_artifacts"
                        " WHERE book_id = :b AND level = :level AND status = 'valid'"
                        " AND (:vid IS NULL AND volume_id IS NULL OR volume_id = :vid)"
                        " ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"b": self._book_id, "level": level, "vid": volume_id},
                )
                .mappings()
                .fetchone()
            )
        return dict(row) if row else None

    def _fetch_summary(self, summary_id: str) -> dict:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT * FROM summary_artifacts WHERE summary_id = :id"
                    ),
                    {"id": summary_id},
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            raise RuntimeError(f"summary {summary_id} 写入失败")
        return dict(row)

    @staticmethod
    def _row_dict(row: dict, *, rebuilt: bool, reused: bool) -> dict:
        d = dict(row)
        d["rebuilt"] = rebuilt
        d["reused"] = reused
        return d

    def _stale_count(self) -> int:
        with self._engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT COUNT(*) FROM summary_artifacts"
                    " WHERE book_id = :b AND status = 'stale'"
                ),
                {"b": self._book_id},
            ).scalar() or 0

    def _book_title(self) -> str:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT title FROM books WHERE book_id = :b"),
                {"b": self._book_id},
            ).fetchone()
        return row[0] if row else self._book_id

    # ── 版本信息 ────────────────────────────────────────────────

    def _prompt_version(self) -> str:
        if isinstance(self._summarizer, LLMSummarizer):
            return self._summarizer.prompt_version
        return DETERMINISTIC_PROMPT_VERSION

    def _profile_id(self) -> str | None:
        if isinstance(self._summarizer, LLMSummarizer):
            return self._summarizer.profile_id or None
        return None
