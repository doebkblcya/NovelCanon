"""实体消歧应用服务（阶段 08，docs/implementation/08 §6）。

把 ResolutionPlan 落库：
- canonical 实体 upsert（首次披露 surface 为内部 canonical name）；
- entity_resolutions 投影（mention → canonical，可重放/重建）；
- entity_mentions.canonical_id 更新为 canonical（投影重写，历史不改写）；
- unresolved_mentions 落库（正式产物）；
- entity_merge_audit：原 mention 实体 → canonical 的合并审计（可追溯）。

全程幂等：同 mention 重复 resolve 不产生重复行/审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from novelcanon.config.hash import stable_config_hash
from novelcanon.resolution.resolver import EntityResolver, ResolutionPlan
from novelcanon.schemas.memory import EntityRecord
from novelcanon.schemas.types import EntityTier
from novelcanon.storage.repository import Repository, now_iso


@dataclass
class ResolveStats:
    mentions: int = 0
    mapped: int = 0
    unresolved: int = 0
    new_entities: int = 0
    merges: int = 0
    errors: list[dict] = field(default_factory=list)


class ResolutionService:
    """实体消歧应用：resolver + 落库（幂等）。"""

    def __init__(self, engine: Engine, *, resolver: EntityResolver | None = None) -> None:
        self._engine = engine
        self._repo = Repository(engine)
        self._resolver = resolver or EntityResolver()

    # ── 对外 ────────────────────────────────────────────────────

    def resolve_run(self, run_id: str, book_id: str) -> ResolveStats:
        """对某 run 的全部 mention 做消歧并落库。"""
        stats = ResolveStats()
        mentions = self._mentions_for_run(run_id)
        if not mentions:
            return stats
        stats.mentions = len(mentions)
        # 预置已知 alias（跨 run 稳定）：库里已有 surface → canonical
        self._resolver.seed(self._known_aliases(book_id))
        plan = self._resolver.resolve(mentions)
        self._apply_plan(run_id, book_id, plan, stats)
        return stats

    # ── 数据读取 ────────────────────────────────────────────────

    def _mentions_for_run(self, run_id: str) -> list[dict]:
        """该 run 的 mention 列表（含 surface / 披露顺序 / 章）。"""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT DISTINCT m.mention_id, m.surface_name, m.chapter_id,"
                        " m.char_start, m.char_end, c.ordinal"
                        " FROM entity_mentions m"
                        " JOIN mention_observations mo ON mo.mention_id = m.mention_id"
                        " JOIN chapters c ON c.chapter_id = m.chapter_id"
                        " WHERE mo.extraction_run_id = :r ORDER BY c.ordinal, m.rowid"
                    ),
                    {"r": run_id},
                )
                .mappings()
                .fetchall()
            )
        return [dict(r) for r in rows]

    def _known_aliases(self, book_id: str) -> dict[str, str]:
        """库里已有 alias：surface → canonical（跨 run 复用，08 §2）。

        同一 surface 多条 alias 时取「最早披露」的 canonical（首次披露
        surface 是 canonical 的内部名，08 §基本原则），保证方向稳定。
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT a.surface_name, a.canonical_id, a.observed_ordinal"
                    " FROM entity_alias_claims a"
                    " JOIN chapters ch ON ch.chapter_id = a.observed_chapter_id"
                    " WHERE ch.book_id = :b"
                    " ORDER BY a.observed_ordinal ASC, a.rowid ASC"
                ),
                {"b": book_id},
            ).fetchall()
        aliases: dict[str, str] = {}
        for surface, canonical, _ordinal in rows:
            aliases.setdefault(surface, canonical)  # 首个（最早披露）胜出
        return aliases

    # ── 应用计划（幂等落库）─────────────────────────────────────

    def _apply_plan(
        self, run_id: str, book_id: str, plan: ResolutionPlan, stats: ResolveStats
    ) -> None:
        for item in plan.resolved:
            assert item.canonical_id is not None
            stats.mapped += 1
            # 1) canonical 实体 upsert（内部 canonical name = 首次披露 surface）
            with self._engine.begin() as conn:
                row = conn.execute(
                    text("SELECT 1 FROM entities WHERE canonical_id = :c"),
                    {"c": item.canonical_id},
                ).fetchone()
            if row is None:
                self._repo.upsert_entity(
                    EntityRecord(
                        canonical_id=item.canonical_id,
                        canonical_name=item.surface_name,
                        tier=EntityTier.MINOR,
                        importance_score=0.0,
                        created_by_run_id=run_id,
                    )
                )
                stats.new_entities += 1
            # 2) merge audit：原 mention 实体 → canonical（目标实体已建，FK 依赖）
            self._record_merge_if_needed(run_id, item, stats)
            # 3) resolution 投影（幂等：mention_id 主键）
            self._write_resolution(
                run_id, item.mention_id, item.canonical_id, plan.resolver_version,
                item.reason,
            )
            # 4) entity_mentions.canonical_id 更新（投影重写）
            self._update_mention_canonical(item.mention_id, item.canonical_id)

        for item in plan.unresolved:
            stats.unresolved += 1
            self._write_unresolved(run_id, book_id, item)

    def _write_resolution(
        self,
        run_id: str,
        mention_id: str,
        canonical_id: str,
        resolver_version: str,
        reason: str,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO entity_resolutions (mention_id,"
                    " canonical_id, resolver_version, reason, run_id, created_at)"
                    " VALUES (:m, :c, :rv, :r, :run, :ts)"
                ),
                {
                    "m": mention_id,
                    "c": canonical_id,
                    "rv": resolver_version,
                    "r": reason,
                    "run": run_id,
                    "ts": now_iso(),
                },
            )

    def _update_mention_canonical(self, mention_id: str, canonical_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE entity_mentions SET canonical_id = :c WHERE mention_id = :m"
                ),
                {"c": canonical_id, "m": mention_id},
            )

    def _record_merge_if_needed(
        self, run_id: str, item: "ResolvedMention", stats: ResolveStats
    ) -> None:
        """若 mention 曾以自身为实体（阶段 07 的章级 namespace 实体），
        记录 merge 审计（from=原实体 → to=canonical），不物理覆盖历史。"""
        assert item.canonical_id is not None
        if item.mention_id == item.canonical_id:
            return  # 本身就是 canonical，无需合并
        with self._engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM entities WHERE canonical_id = :m"),
                {"m": item.mention_id},
            ).fetchone()
        if exists is None:
            return  # 原 mention 实体不存在（已是 canonical 引用），无需审计
        # 幂等：同一 from→to 已审计过则跳过（无唯一约束，显式查重）
        with self._engine.connect() as conn:
            dup = conn.execute(
                text(
                    "SELECT 1 FROM entity_merge_audit"
                    " WHERE from_entity_id = :f AND to_entity_id = :t AND run_id = :r"
                    " LIMIT 1"
                ),
                {"f": item.mention_id, "t": item.canonical_id, "r": run_id},
            ).fetchone()
        if dup is not None:
            return
        self._repo.record_merge(
            "merge",
            from_entity_id=item.mention_id,
            to_entity_id=item.canonical_id,
            run_id=run_id,
            reason=item.reason,
        )
        stats.merges += 1

    def _write_unresolved(
        self, run_id: str, book_id: str, item: "ResolvedMention"
    ) -> None:
        # 找到 mention 的章内位置（char_start/char_end/context）
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT chapter_id, char_start, char_end FROM entity_mentions"
                    " WHERE mention_id = :m"
                ),
                {"m": item.mention_id},
            ).fetchone()
        if row is None:
            return
        unresolved_id = "unres_" + stable_config_hash(
            {"run": run_id, "mention": item.mention_id}
        )[:16]
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO unresolved_mentions (unresolved_id,"
                    " surface_name, chapter_id, char_start, char_end, context, reason,"
                    " run_id, created_at)"
                    " VALUES (:u, :s, :c, :cs, :ce, '', :r, :run, :ts)"
                ),
                {
                    "u": unresolved_id,
                    "s": item.surface_name,
                    "c": row[0],
                    "cs": row[1] or 0,
                    "ce": row[2] or 0,
                    "r": item.reason,
                    "run": run_id,
                    "ts": now_iso(),
                },
            )
