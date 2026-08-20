"""阶段 10 卷分组测试（docs/implementation/10 §6）。

覆盖验证项：
- 优先原书卷标题（source 分组）；缺失时默认每 50 章分组；
- volume_id 持久化 UUID；
- 保存来源/边界/ordinal/grouping version/content hash；
- 分组变化生成新版本并使旧摘要失效；
- 幂等：边界与内容未变时返回现有分组（不重建）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, text

from novelcanon.summaries import (
    VolumeGrouper,
    list_active_volumes,
)
from tests.helpers import seed_active_book


def _many_chapter_book(migrated_db: Engine, tmp_path: Path, n: int = 120) -> str:
    """导入 n 章（无卷结构）的测试书，用于默认分组。"""
    from novelcanon.ingestion.service import import_book
    from tests.helpers import make_fixture_epub

    chapters = [
        (f"第{i}章", "在青石台上，萧炎盘膝而坐，周身灵气汇聚，修炼之声连绵不绝。" * 3)
        for i in range(1, n + 1)
    ]
    epub = tmp_path / "many.epub"
    make_fixture_epub(epub, chapters, title="多章书")
    result = import_book(migrated_db, epub, book_id="book_many")
    return result.book_id


def test_default_grouping_50_chapters(tmp_path: Path, migrated_db: Engine) -> None:
    """缺失原书卷：默认每 50 章一组（120 章 → 3 卷）。"""
    book_id = _many_chapter_book(migrated_db, tmp_path, n=120)
    result = VolumeGrouper(migrated_db, book_id).group()
    assert result.source == "default"
    assert len(result.volumes) == 3
    assert result.volumes[0].start_ordinal == 0  # ordinal 0 基（系统约定）
    assert result.volumes[0].end_ordinal == 49
    assert result.volumes[2].start_ordinal == 100
    assert result.volumes[2].end_ordinal == 119
    # 保存来源/边界/ordinal/grouping version/content hash
    row = list_active_volumes(migrated_db, book_id)[0]
    assert row["grouping_source"] == "default"
    assert row["start_chapter_id"] and row["end_chapter_id"]
    assert row["content_hash"]


def test_grouping_idempotent(tmp_path: Path, migrated_db: Engine) -> None:
    """幂等：内容未变时重复分组返回现有分组（不重建）。"""
    book_id = _many_chapter_book(migrated_db, tmp_path, n=60)
    g1 = VolumeGrouper(migrated_db, book_id).group()
    g2 = VolumeGrouper(migrated_db, book_id).group()
    assert not g2.rebuilt
    assert g1.grouping_version == g2.grouping_version
    assert [v.volume_id for v in g1.volumes] == [v.volume_id for v in g2.volumes]


def test_grouping_rebuild_on_chapter_change(tmp_path: Path, migrated_db: Engine) -> None:
    """章节变化 → 分组重建新版本，旧卷摘要失效（10 §6）。"""
    book_id = _many_chapter_book(migrated_db, tmp_path, n=60)
    g1 = VolumeGrouper(migrated_db, book_id).group()
    # 伪造一条旧版本卷摘要
    with migrated_db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO summary_artifacts (summary_id, book_id, level, volume_id,"
                " grouping_version, title, content, input_claim_versions,"
                " depends_on_summaries, prompt_version, schema_version, content_hash,"
                " max_observed_ordinal, status, created_at)"
                " VALUES ('sum_old', :b, 'volume', :vid, :gv, '旧卷', '旧内容', '[]',"
                " '[]', 'deterministic-v1', 'reducer-v1', 'hash_old', 30, 'valid', :ts)"
            ),
            {
                "b": book_id,
                "vid": g1.volumes[0].volume_id,
                "gv": g1.grouping_version,
                "ts": "2026-01-01T00:00:00+00:00",
            },
        )
    # 新增章节（61 章）→ 边界变化 → 重建
    from novelcanon.ingestion.service import import_book
    from tests.helpers import make_fixture_epub

    chapters = [
        (f"第{i}章", "在青石台上，萧炎盘膝而坐，周身灵气汇聚，修炼之声连绵不绝。" * 3)
        for i in range(1, 62)
    ]
    epub = tmp_path / "more.epub"
    make_fixture_epub(epub, chapters, title="多章书")
    import_book(migrated_db, epub, book_id=book_id)
    g2 = VolumeGrouper(migrated_db, book_id).group()
    assert g2.rebuilt
    assert g2.grouping_version != g1.grouping_version
    # 旧卷摘要被标 stale
    with migrated_db.connect() as conn:
        status = conn.execute(
            text(
                "SELECT status FROM summary_artifacts WHERE summary_id = 'sum_old'"
            )
        ).scalar()
    assert status == "stale"


def test_source_grouping_preferred(tmp_path: Path, migrated_db: Engine) -> None:
    """原书卷标题优先：带卷结构的书走 source 分组。"""
    data = seed_active_book(migrated_db, tmp_path)
    result = VolumeGrouper(migrated_db, data["book_id"]).group()
    assert result.source == "default"  # fixture 无卷结构 → 默认
    # 模拟导入时已有卷行：补 source 卷行后走 source 分组
    from novelcanon.ingestion.service import import_book
    from tests.helpers import FIXTURE_CHAPTERS, make_fixture_epub

    epub = tmp_path / "vol.epub"
    make_fixture_epub(epub, FIXTURE_CHAPTERS, title="带卷")
    import_book(migrated_db, epub, book_id="book_vol")
    with migrated_db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO volumes (volume_id, book_id, title, ordinal,"
                " grouping_version, created_at)"
                " VALUES ('vol_v1', 'book_vol', '第一卷', 1, '1', :ts)"
            ),
            {"ts": "2026-01-01T00:00:00+00:00"},
        )
        conn.execute(
            text(
                "UPDATE chapters SET volume_id = 'vol_v1'"
                " WHERE book_id = 'book_vol' AND ordinal <= 2"
            )
        )
    r = VolumeGrouper(migrated_db, "book_vol").group()
    assert r.source == "source"
    assert r.volumes[0].title == "第一卷"
