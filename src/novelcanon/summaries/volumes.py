"""卷分组器（阶段 10 §6，docs/implementation/10）。

- 优先原书卷标题（导入时 EPUB 目录卷结构）；缺失时默认每 50 章分组；
- volume_id 使用持久化 UUID（不因序号/版本推导）；
- 卷行保存来源（source/default）、边界（start/end chapter）、ordinal、
  grouping version 与 content hash（分组输入内容的 hash）；
- 分组变化生成新版本（新 grouping_version + 新卷行 + chapters 重新指向），
  并使旧分组版本的卷摘要失效（10 §6「分组变化生成新版本并使旧摘要失效」）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from novelcanon.config.hash import stable_config_hash
from novelcanon.schemas.ids import new_uuid_id
from novelcanon.storage.repository import Repository, now_iso

GROUPING_VERSION = "grouping-v1"  # 分组策略版本（分组算法变化时递增）
DEFAULT_CHAPTERS_PER_VOLUME = 50


@dataclass(frozen=True)
class VolumeGroup:
    volume_id: str
    title: str
    ordinal: int
    start_chapter_id: str | None
    end_chapter_id: str | None
    start_ordinal: int
    end_ordinal: int
    grouping_source: str  # 'source' | 'default'


@dataclass(frozen=True)
class GroupingResult:
    book_id: str
    grouping_version: str
    source: str
    volumes: list[VolumeGroup] = field(default_factory=list)
    rebuilt: bool = False
    changed: bool = False


class VolumeGrouper:
    """book_id 绑定的卷分组（幂等：边界与内容未变时返回现有分组）。"""

    def __init__(
        self,
        engine: Engine,
        book_id: str,
        *,
        chapters_per_volume: int = DEFAULT_CHAPTERS_PER_VOLUME,
    ) -> None:
        self._engine = engine
        self._book_id = book_id
        self._chapters_per_volume = max(1, chapters_per_volume)
        self._repo = Repository(engine)

    # ── 对外 ────────────────────────────────────────────────────

    def group(self) -> GroupingResult:
        """确保分组最新：边界/内容变化时重建新版本并失效旧摘要。"""
        chapters = self._chapters()
        if not chapters:
            raise ValueError(f"book={self._book_id} 没有章节，无法分组")
        source = self._detect_source(chapters)
        boundaries = self._expected_boundaries(chapters, source)
        content_hash = self._content_hash(chapters, source, boundaries)
        current = self._current_grouping()
        current_hash = current[0]["content_hash"] if current else None

        if current and current_hash == content_hash:
            # 边界与内容一致 → 幂等返回（rebuilt=False）
            return GroupingResult(
                book_id=self._book_id,
                grouping_version=current[0]["grouping_version"],
                source=source,
                volumes=self._to_groups(current),
            )

        if current and all(v["content_hash"] is None for v in current) and (
            self._boundaries_match(current, boundaries)
        ):
            # 旧版导入卷行缺 content_hash：边界匹配则补全，不重建版本
            self._backfill(current, boundaries, source, content_hash)
            return GroupingResult(
                book_id=self._book_id,
                grouping_version=current[0]["grouping_version"],
                source=source,
                volumes=self._to_groups(self._current_grouping()),
                rebuilt=False,
                changed=True,
            )

        # 重建新版本：新 grouping_version + 新卷行 + chapters 重指向
        next_version = self._next_version(current)
        new_volumes: list[VolumeGroup] = []
        with self._engine.begin() as conn:
            for group in boundaries:
                vid = new_uuid_id("vol")
                conn.execute(
                    text(
                        "INSERT INTO volumes (volume_id, book_id, title, ordinal,"
                        " start_chapter_id, end_chapter_id, grouping_version,"
                        " content_hash, grouping_source, created_at)"
                        " VALUES (:id, :b, :title, :ord, :sc, :ec, :gv, :hash,"
                        " :src, :ts)"
                    ),
                    {
                        "id": vid,
                        "b": self._book_id,
                        "title": group["title"],
                        "ord": group["ordinal"],
                        "sc": group["start_chapter_id"],
                        "ec": group["end_chapter_id"],
                        "gv": next_version,
                        "hash": content_hash,
                        "src": source,
                        "ts": now_iso(),
                    },
                )
                new_volumes.append(
                    VolumeGroup(
                        volume_id=vid,
                        title=group["title"],
                        ordinal=group["ordinal"],
                        start_chapter_id=group["start_chapter_id"],
                        end_chapter_id=group["end_chapter_id"],
                        start_ordinal=group["start_ordinal"],
                        end_ordinal=group["end_ordinal"],
                        grouping_source=source,
                    )
                )
                # chapters 重新指向新卷（按 ordinal 范围）
                conn.execute(
                    text(
                        "UPDATE chapters SET volume_id = :vid"
                        " WHERE book_id = :b AND ordinal >= :s AND ordinal <= :e"
                    ),
                    {
                        "vid": vid,
                        "b": self._book_id,
                        "s": group["start_ordinal"],
                        "e": group["end_ordinal"],
                    },
                )
        # 旧分组版本的卷摘要失效（10 §6）
        self._stale_summaries_for_old_versions(next_version)
        return GroupingResult(
            book_id=self._book_id,
            grouping_version=next_version,
            source=source,
            volumes=new_volumes,
            rebuilt=True,
            changed=True,
        )

    # ── 数据读取 ────────────────────────────────────────────────

    def _chapters(self) -> list[dict]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT chapter_id, ordinal, title, volume_id, content_hash"
                        " FROM chapters WHERE book_id = :b ORDER BY ordinal"
                    ),
                    {"b": self._book_id},
                )
                .mappings()
                .fetchall()
            )
        return [dict(r) for r in rows]

    def _current_grouping(self) -> list[dict]:
        """当前最新分组版本的卷行（grouping_version 最大的一组）。

        经 chapters 补出 start/end ordinal 范围（幂等返回与摘要共用）。
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT v.*, sc.ordinal AS start_ordinal,"
                        " ec.ordinal AS end_ordinal"
                        " FROM volumes v"
                        " LEFT JOIN chapters sc ON sc.chapter_id = v.start_chapter_id"
                        " LEFT JOIN chapters ec ON ec.chapter_id = v.end_chapter_id"
                        " WHERE v.book_id = :b AND v.grouping_version = ("
                        "  SELECT MAX(grouping_version) FROM volumes WHERE book_id = :b)"
                        " ORDER BY v.ordinal"
                    ),
                    {"b": self._book_id},
                )
                .mappings()
                .fetchall()
            )
        return [dict(r) for r in rows]

    # ── 分组计算 ────────────────────────────────────────────────

    def _detect_source(self, chapters: list[dict]) -> str:
        """优先原书卷标题：source/default 的来源判定。

        - 已有分组版本时保持既有来源（重建会把 chapters.volume_id
          指向新卷行——若只看 volume_id 会被误判为 source）；
        - 首次分组：导入时章节有 volume_id 引用（原书卷）→ source，
          否则 default（每 50 章）。
        """
        current = self._current_grouping()
        if current:
            src = current[0].get("grouping_source")
            if src in ("source", "default"):
                return src
        return "source" if any(ch["volume_id"] for ch in chapters) else "default"

    def _expected_boundaries(
        self, chapters: list[dict], source: str
    ) -> list[dict]:
        """期望分组边界：[{title, ordinal, start/end chapter_id, start/end ordinal}]。"""
        if source == "source":
            return self._source_boundaries(chapters)
        return self._default_boundaries(chapters)

    def _source_boundaries(self, chapters: list[dict]) -> list[dict]:
        """按章节 volume_id 分组（保持 ordinal 序；同卷章节必须连续）。"""
        existing = {
            v["volume_id"]: v
            for v in self._current_grouping()
        }
        groups: list[dict] = []
        current_vid: str | None = None
        for ch in chapters:
            vid = ch["volume_id"]
            if vid != current_vid:
                current_vid = vid
                groups.append(
                    {
                        "volume_id": vid,
                        "title": existing.get(vid, {}).get("title") or f"第 {len(groups) + 1} 卷",
                        "ordinal": len(groups) + 1,
                        "start_chapter_id": ch["chapter_id"],
                        "end_chapter_id": ch["chapter_id"],
                        "start_ordinal": ch["ordinal"],
                        "end_ordinal": ch["ordinal"],
                    }
                )
            else:
                g = groups[-1]
                g["end_chapter_id"] = ch["chapter_id"]
                g["end_ordinal"] = ch["ordinal"]
        return groups

    def _default_boundaries(self, chapters: list[dict]) -> list[dict]:
        """默认每 50 章一组（10 §6），标题「第 N 卷」。"""
        size = self._chapters_per_volume
        groups: list[dict] = []
        for i, ch in enumerate(chapters):
            gidx = i // size
            if gidx >= len(groups):
                groups.append(
                    {
                        "title": f"第 {gidx + 1} 卷",
                        "ordinal": gidx + 1,
                        "start_chapter_id": ch["chapter_id"],
                        "end_chapter_id": ch["chapter_id"],
                        "start_ordinal": ch["ordinal"],
                        "end_ordinal": ch["ordinal"],
                    }
                )
            else:
                g = groups[gidx]
                g["end_chapter_id"] = ch["chapter_id"]
                g["end_ordinal"] = ch["ordinal"]
        return groups

    def _content_hash(
        self, chapters: list[dict], source: str, boundaries: list[dict]
    ) -> str:
        """分组内容 hash：来源 + 边界 + 每章 content_hash（输入变化即变化）。"""
        return stable_config_hash(
            {
                "policy": GROUPING_VERSION,
                "source": source,
                "boundaries": [
                    [g["start_ordinal"], g["end_ordinal"], g["title"]] for g in boundaries
                ],
                "chapters": [
                    (c["ordinal"], c["content_hash"] or "") for c in chapters
                ],
            }
        )

    def _boundaries_match(self, current: list[dict], boundaries: list[dict]) -> bool:
        if len(current) != len(boundaries):
            return False
        return all(
            v["ordinal"] == g["ordinal"] and v["title"] == g["title"]
            for v, g in zip(current, boundaries, strict=False)
        )

    def _next_version(self, current: list[dict]) -> str:
        if not current:
            return "1"
        try:
            return str(int(current[0]["grouping_version"]) + 1)
        except (TypeError, ValueError):
            return f"{current[0]['grouping_version']}-{len(current)}"

    # ── 补全 / 失效 ─────────────────────────────────────────────

    def _backfill(
        self,
        current: list[dict],
        boundaries: list[dict],
        source: str,
        content_hash: str,
    ) -> None:
        """旧导入卷行缺边界/来源/hash：同版本内补全（不重建版本）。"""
        by_ordinal = {v["ordinal"]: v for v in current}
        with self._engine.begin() as conn:
            for g in boundaries:
                v = by_ordinal.get(g["ordinal"])
                if v is None:
                    continue
                conn.execute(
                    text(
                        "UPDATE volumes SET start_chapter_id = :sc, end_chapter_id = :ec,"
                        " content_hash = :hash, grouping_source = :src"
                        " WHERE volume_id = :vid"
                    ),
                    {
                        "sc": g["start_chapter_id"],
                        "ec": g["end_chapter_id"],
                        "hash": content_hash,
                        "src": source,
                        "vid": v["volume_id"],
                    },
                )

    def _stale_summaries_for_old_versions(self, keep_version: str) -> None:
        """旧分组版本的卷摘要标 stale（10 §6「旧摘要失效」）。"""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE summary_artifacts SET status = 'stale'"
                    " WHERE book_id = :b AND level = 'volume' AND status = 'valid'"
                    " AND (grouping_version IS NULL OR grouping_version != :gv)"
                ),
                {"b": self._book_id, "gv": keep_version},
            )

    @staticmethod
    def _to_groups(rows: list[dict]) -> list[VolumeGroup]:
        return [
            VolumeGroup(
                volume_id=r["volume_id"],
                title=r["title"],
                ordinal=r["ordinal"],
                start_chapter_id=r["start_chapter_id"],
                end_chapter_id=r["end_chapter_id"],
                start_ordinal=r.get("start_ordinal") or 0,
                end_ordinal=r.get("end_ordinal") or 0,
                grouping_source=r.get("grouping_source") or "default",
            )
            for r in rows
        ]


def list_active_volumes(engine: Engine, book_id: str) -> list[dict]:
    """当前生效分组（grouping_version 最大）的卷行（CLI/摘要用）。"""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT volume_id, title, ordinal, start_chapter_id, end_chapter_id,"
                    " grouping_version, content_hash, grouping_source"
                    " FROM volumes WHERE book_id = :b AND grouping_version = ("
                    "  SELECT MAX(grouping_version) FROM volumes WHERE book_id = :b)"
                    " ORDER BY ordinal"
                ),
                {"b": book_id},
            )
            .mappings()
            .fetchall()
        )
    return [dict(r) for r in rows]
