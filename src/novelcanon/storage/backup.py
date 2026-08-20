"""备份、恢复与完整性校验（阶段 11 P3 前置，docs/implementation/11 §P3）。

- backup_database：SQLite 在线备份（WAL 安全，sqlite3.Connection.backup），
  附带元数据清单（book/章节/claims 数、时间）；
- restore_database：把备份复制回目标路径并重跑完整性校验；
- verify_integrity：外键完整性 + 证据 span hash 复现 + claims 幂等抽查
  （11 验证项「数据库恢复后通过 foreign key 和证据完整性检查」）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, text

from novelcanon.storage.repository import now_iso


@dataclass(frozen=True)
class BackupResult:
    backup_path: Path
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class IntegrityReport:
    fk_violations: int
    evidence_checked: int
    evidence_reproduced: int
    claim_duplicates: int
    ok: bool = True

    def as_dict(self) -> dict:
        return {
            "fk_violations": self.fk_violations,
            "evidence_checked": self.evidence_checked,
            "evidence_reproduced": self.evidence_reproduced,
            "claim_duplicates": self.claim_duplicates,
            "ok": self.ok,
        }


def backup_database(engine: Engine, dest: Path) -> BackupResult:
    """在线备份（WAL 安全）：当前库 → dest 文件，附元数据。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = engine.raw_connection().driver_connection  # sqlite3.Connection
    assert src is not None, "SQLite 驱动连接不可用"
    dest_conn = sqlite3.connect(str(dest))
    try:
        with dest_conn:
            src.backup(dest_conn)
    finally:
        dest_conn.close()

    with engine.connect() as conn:
        meta = {
            "books": conn.execute(text("SELECT COUNT(*) FROM books")).scalar(),
            "chapters": conn.execute(text("SELECT COUNT(*) FROM chapters")).scalar(),
            "claims": conn.execute(text("SELECT COUNT(*) FROM claims")).scalar(),
            "evidence": conn.execute(text("SELECT COUNT(*) FROM claim_evidence")).scalar(),
            "created_at": now_iso(),
        }
    return BackupResult(backup_path=dest, meta=meta)


def restore_database(engine: Engine, backup_path: Path, db_path: Path) -> IntegrityReport:
    """把备份恢复到 db_path（先关闭引擎连接），并校验**目标库**完整性。

    校验对象是刚恢复的 db_path（独立引擎），而不是原 engine 指向的库
    （P0：恢复验收必须验证恢复产物本身）。
    """
    engine.dispose()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 在线恢复：备份 → 目标库
    src = sqlite3.connect(str(backup_path))
    dest = sqlite3.connect(str(db_path))
    try:
        with dest:
            src.backup(dest)
    finally:
        dest.close()
        src.close()

    from novelcanon.storage.engine import create_db_engine

    target = create_db_engine(db_path)
    try:
        return verify_integrity(target, evidence_sample=None)
    finally:
        target.dispose()


def verify_integrity(engine: Engine, *, evidence_sample: int | None = 50) -> IntegrityReport:
    """完整性校验（11 验证项）。

    - 外键完整性：PRAGMA foreign_key_check 行数；
    - 证据复现：抽查 claim_evidence，用章节原文重算 span_hash
      （evidence_sample=None 时校验全部证据）；
    - claims 幂等：主键无重复（SQLite 主键天然保证，统计行数兜底）。

    ok = 无外键违规 + 无重复 claim + 已检查证据**全部**复现
    （P0：证据 hash 全部损坏时不得报告通过）。
    """
    with engine.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
        fk_count = len(fk)

        limit_sql = "" if evidence_sample is None else " LIMIT :n"
        rows = conn.execute(
            text(
                "SELECT e.span_hash, e.chapter_id, e.char_start, e.char_end"
                " FROM claim_evidence e ORDER BY rowid" + limit_sql
            ),
            {"n": evidence_sample} if evidence_sample is not None else {},
        ).fetchall()
        evidence_checked = 0
        evidence_reproduced = 0
        for span_hash, chapter_id, char_start, char_end in rows:
            evidence_checked += 1
            ch = conn.execute(
                text("SELECT c.char_start, c.char_end FROM chapters c WHERE c.chapter_id = :cid"),
                {"cid": chapter_id},
            ).fetchone()
            if ch is None:
                continue
            # 章节在全书文本中的绝对区间
            book_text = conn.execute(
                text(
                    "SELECT b.normalized_text FROM chapters c"
                    " JOIN books b ON b.book_id = c.book_id"
                    " WHERE c.chapter_id = :cid"
                ),
                {"cid": chapter_id},
            ).fetchone()
            if book_text is None or book_text[0] is None:
                continue
            abs_start = ch[0] + char_start
            abs_end = ch[0] + char_end
            span = book_text[0][abs_start:abs_end]
            from novelcanon.ingestion.normalize import sha256

            if span and sha256(span) == span_hash:
                evidence_reproduced += 1

        claim_duplicates = conn.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT claim_version_id FROM claims"
                " GROUP BY claim_version_id HAVING COUNT(*) > 1)"
            )
        ).scalar()

    evidence_ok = evidence_checked == 0 or evidence_reproduced == evidence_checked
    ok = fk_count == 0 and int(claim_duplicates or 0) == 0 and evidence_ok
    return IntegrityReport(
        fk_violations=fk_count,
        evidence_checked=evidence_checked,
        evidence_reproduced=evidence_reproduced,
        claim_duplicates=int(claim_duplicates or 0),
        ok=ok,
    )
