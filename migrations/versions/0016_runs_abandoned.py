"""阶段 11 复审：RunStatus 增加 abandoned（人工放弃，与 failed 区分）。

Revision ID: 0016_runs_abandoned
Revises: 0015_ledger_run_optional

背景：真实语料运行（百年孤独全量建库）遗留 2 个 running run（开发抽查 +
超时后由新 run 取代），缺少「人工放弃」状态，陈旧 run 污染运维统计。
abandoned 与 failed（执行失败）语义区分；active run 禁止放弃。

SQLite 不支持 ALTER CHECK——按 0003 的方式重建 extraction_runs 表，
CHECK 值域加入 'abandoned'。重建父表期间临时关闭外键约束，
结束后恢复。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_runs_abandoned"
down_revision = "0015_ledger_run_optional"
branch_labels = None
depends_on = None

_RUN_STATUSES = (
    "created', 'running', 'validating', 'ready_to_activate',"
    " 'active', 'failed', 'retrying', 'superseded', 'abandoned"
)


def upgrade() -> None:
    # SQLite 重建被引用父表：临时关闭外键检查（默认 pragma 在连接级）
    op.execute("PRAGMA foreign_keys=OFF")
    op.execute("DROP VIEW IF EXISTS v_active_claims")

    op.create_table(
        "extraction_runs_v2",
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint(f"status IN ('{_RUN_STATUSES}')", name="ck_runs_status"),
            nullable=False,
            server_default="created",
        ),
        sa.Column("input_hash", sa.Text, nullable=True),
        sa.Column("pipeline_version", sa.Text, nullable=True),
        sa.Column("prompt_version", sa.Text, nullable=True),
        sa.Column("schema_version", sa.Text, nullable=True),
        sa.Column("compression_version", sa.Text, nullable=True),
        sa.Column("generation_profile_id", sa.Text, nullable=True),
        sa.Column("embedding_profile_id", sa.Text, nullable=True),
        sa.Column("config_hash", sa.Text, nullable=True),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.execute(
        "INSERT INTO extraction_runs_v2 (run_id, book_id, status, input_hash,"
        " pipeline_version, prompt_version, schema_version, compression_version,"
        " generation_profile_id, embedding_profile_id, config_hash, started_at,"
        " finished_at, error)"
        " SELECT run_id, book_id, status, input_hash, pipeline_version, prompt_version,"
        " schema_version, compression_version, generation_profile_id,"
        " embedding_profile_id, config_hash, started_at, finished_at, error"
        " FROM extraction_runs"
    )
    op.drop_table("extraction_runs")
    op.rename_table("extraction_runs_v2", "extraction_runs")
    op.create_index("ix_runs_book_status", "extraction_runs", ["book_id", "status"])

    op.execute(
        "CREATE VIEW IF NOT EXISTS v_active_claims AS "
        "SELECT c.rowid AS _rowid, c.*, r.book_id AS book_id "
        "FROM claims c "
        "JOIN claim_observations o ON o.claim_version_id = c.claim_version_id "
        "JOIN extraction_runs r ON r.run_id = o.extraction_run_id "
        "WHERE r.status = 'active'"
    )
    op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    op.execute("PRAGMA foreign_keys=OFF")
    op.execute("DROP VIEW IF EXISTS v_active_claims")
    # 还原旧值域（无 abandoned；已 abandoned 的 run 归入 failed）
    op.create_table(
        "extraction_runs_v1",
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint(
                "status IN ('created','running','validating','ready_to_activate',"
                " 'active','failed','retrying','superseded')",
                name="ck_runs_status",
            ),
            nullable=False,
            server_default="created",
        ),
        sa.Column("input_hash", sa.Text, nullable=True),
        sa.Column("pipeline_version", sa.Text, nullable=True),
        sa.Column("prompt_version", sa.Text, nullable=True),
        sa.Column("schema_version", sa.Text, nullable=True),
        sa.Column("compression_version", sa.Text, nullable=True),
        sa.Column("generation_profile_id", sa.Text, nullable=True),
        sa.Column("embedding_profile_id", sa.Text, nullable=True),
        sa.Column("config_hash", sa.Text, nullable=True),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.execute(
        "INSERT INTO extraction_runs_v1 (run_id, book_id, status, input_hash,"
        " pipeline_version, prompt_version, schema_version, compression_version,"
        " generation_profile_id, embedding_profile_id, config_hash, started_at,"
        " finished_at, error)"
        " SELECT run_id, book_id,"
        " CASE WHEN status = 'abandoned' THEN 'failed' ELSE status END,"
        " input_hash, pipeline_version, prompt_version, schema_version,"
        " compression_version, generation_profile_id, embedding_profile_id,"
        " config_hash, started_at, finished_at, error"
        " FROM extraction_runs"
    )
    op.drop_table("extraction_runs")
    op.rename_table("extraction_runs_v1", "extraction_runs")
    op.create_index("ix_runs_book_status", "extraction_runs", ["book_id", "status"])
    op.execute(
        "CREATE VIEW IF NOT EXISTS v_active_claims AS "
        "SELECT c.rowid AS _rowid, c.*, r.book_id AS book_id "
        "FROM claims c "
        "JOIN claim_observations o ON o.claim_version_id = c.claim_version_id "
        "JOIN extraction_runs r ON r.run_id = o.extraction_run_id "
        "WHERE r.status = 'active'"
    )
    op.execute("PRAGMA foreign_keys=ON")
