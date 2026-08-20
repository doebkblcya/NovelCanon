"""阶段 11 备份恢复测试（docs/implementation/11 §P3/P5 验证项）。

覆盖验证项：
- 备份（在线，WAL 安全）→ 删除原库 → 恢复 → 数据一致；
- 恢复后通过 foreign key 与证据完整性检查；
- 证据 span hash 复现率（抽样）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, text

from novelcanon.storage.backup import (
    backup_database,
    restore_database,
    verify_integrity,
)
from tests.helpers import seed_active_book


def test_backup_restore_roundtrip(tmp_path: Path, migrated_db: Engine) -> None:
    """备份 → 删库 → 恢复 → 数据一致 + 完整性通过。"""
    data = seed_active_book(migrated_db, tmp_path)
    db_path = tmp_path / "novelcanon_test.db"
    backup_path = tmp_path / "backup.db"

    result = backup_database(migrated_db, backup_path)
    assert result.backup_path.exists()
    assert result.meta["books"] >= 1
    assert result.meta["claims"] > 0

    # 删除原库（模拟灾难）
    db_path.unlink()
    assert not db_path.exists()

    # 恢复 + 完整性校验
    report = restore_database(migrated_db, backup_path, db_path)
    assert report.fk_violations == 0, f"外键完整性失败：{report.fk_violations}"
    assert report.claim_duplicates == 0
    assert report.evidence_checked > 0
    assert report.evidence_reproduced == report.evidence_checked, (
        f"证据 hash 复现不完整：{report.evidence_reproduced}/{report.evidence_checked}"
    )
    assert report.ok

    # 恢复后可查询
    from novelcanon.query import QueryService

    qs = QueryService(migrated_db, data["book_id"])
    assert qs.display_name("ent_xiaoyan") == "萧炎"
    assert qs.entity_state("ent_xiaoyan")


def test_verify_integrity_detects_fk_breakage(tmp_path: Path, migrated_db: Engine) -> None:
    """完整性校验能发现外键破坏（删除引用的章节）。"""
    data = seed_active_book(migrated_db, tmp_path)
    # 关闭 FK 约束后破坏引用（模拟损坏库）
    with migrated_db.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
    with migrated_db.begin() as conn:
        conn.execute(
            text("DELETE FROM chapters WHERE chapter_id = :c"),
            {"c": data["chapters"][0]},
        )
    report = verify_integrity(migrated_db)
    assert not report.ok
    assert report.fk_violations > 0


def test_verify_integrity_detects_corrupted_evidence_hash(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """P0：证据 span hash 全部损坏时完整性必须判失败（不得假通过）。"""
    seed_active_book(migrated_db, tmp_path)
    # 篡改全部证据 hash（模拟备份/存储损坏）
    with migrated_db.begin() as conn:
        conn.execute(text("UPDATE claim_evidence SET span_hash = 'deadbeef'"))
    report = verify_integrity(migrated_db, evidence_sample=None)
    assert report.evidence_checked > 0
    assert report.evidence_reproduced == 0
    assert not report.ok, "损坏证据 hash 不得报告通过"
    # 部分损坏同样检出（抽样也要求已检查证据全部复现）
    report2 = verify_integrity(migrated_db, evidence_sample=3)
    assert not report2.ok


def test_restore_verifies_target_db(tmp_path: Path, migrated_db: Engine) -> None:
    """P0：恢复到不同 db_path 时，校验的是恢复出的目标库。"""
    data = seed_active_book(migrated_db, tmp_path)
    backup_path = tmp_path / "backup.db"
    backup_database(migrated_db, backup_path)
    target = tmp_path / "restored" / "target.db"
    report = restore_database(migrated_db, backup_path, target)
    assert report.evidence_checked > 0
    assert report.evidence_reproduced == report.evidence_checked
    assert report.ok
    # 目标库确实可查询且数据一致
    from novelcanon.query import QueryService
    from novelcanon.storage.engine import create_db_engine

    qs = QueryService(create_db_engine(target), data["book_id"])
    assert qs.display_name("ent_xiaoyan") == "萧炎"
