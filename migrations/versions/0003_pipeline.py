"""阶段 04：run 状态机扩展、checkpoint 与 Token 账本表。

Revision ID: 0003_pipeline
Revises: 0002_raw_chunks

- extraction_runs：状态值域扩展为显式状态机（created/running/validating/
  ready_to_activate/active/failed/retrying/superseded），SQLite 需重建表；
- run_checkpoints：章节级 checkpoint，唯一键 = (book_id, chapter_id,
  content_hash, pipeline/prompt/compression/schema version)；
- token_ledger：每次模型调用计量（input/cached/reasoning/output/retry/
  discarded + provider/model/profile + book/run/chapter/stage）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_pipeline"
down_revision = "0002_raw_chunks"
branch_labels = None
depends_on = None

_RUN_STATUSES = (
    "created', 'running', 'validating', 'ready_to_activate',"
    " 'active', 'failed', 'retrying', 'superseded"
)


def upgrade() -> None:
    # v_active_claims 引用 extraction_runs；SQLite 重建父表前必须先 drop 视图
    op.execute("DROP VIEW IF EXISTS v_active_claims")

    # ── extraction_runs 重建（状态机值域，SQLite 不支持 ALTER CHECK）──
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

    # ── 章节 checkpoint ────────────────────────────────────────
    op.create_table(
        "run_checkpoints",
        sa.Column("checkpoint_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Text, sa.ForeignKey("extraction_runs.run_id"), nullable=False),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column(
            "chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=False,
        ),
        sa.Column("checkpoint_key", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("pipeline_version", sa.Text, nullable=False, server_default=""),
        sa.Column("prompt_version", sa.Text, nullable=False, server_default=""),
        sa.Column("compression_version", sa.Text, nullable=False, server_default=""),
        sa.Column("schema_version", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False, server_default="done"),
        sa.Column("payload", sa.Text, nullable=False, server_default="{}"),
        sa.Column("source_run_id", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint("run_id", "checkpoint_key", name="uq_run_checkpoint_key"),
    )
    op.create_index("ix_checkpoints_run", "run_checkpoints", ["run_id"])
    op.create_index("ix_checkpoints_chapter", "run_checkpoints", ["book_id", "chapter_id"])

    # ── Token 账本 ─────────────────────────────────────────────
    op.create_table(
        "token_ledger",
        sa.Column("ledger_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Text, sa.ForeignKey("extraction_runs.run_id"), nullable=False),
        sa.Column("book_id", sa.Text, nullable=False),
        sa.Column("chapter_id", sa.Text, nullable=True),
        sa.Column("stage", sa.Text, nullable=False, server_default="map"),
        sa.Column("provider", sa.Text, nullable=True),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("profile_id", sa.Text, nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("discarded_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_ledger_run_stage", "token_ledger", ["run_id", "stage"])

    # ── 重建 active 视图（0001 定义，父表重建后需恢复）────────────
    op.execute(
        "CREATE VIEW IF NOT EXISTS v_active_claims AS "
        "SELECT c.rowid AS _rowid, c.* FROM claims c "
        "JOIN extraction_runs r ON c.created_by_run_id = r.run_id "
        "WHERE r.status = 'active'"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_active_claims")
    op.drop_table("token_ledger")
    op.drop_table("run_checkpoints")
    # 还原旧状态值域（running/failed/active/superseded）
    op.create_table(
        "extraction_runs_v1",
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint(
                "status IN ('running','failed','active','superseded')",
                name="ck_runs_status",
            ),
            nullable=False,
            server_default="running",
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
        " SELECT run_id, book_id, status, input_hash, pipeline_version, prompt_version,"
        " schema_version, compression_version, generation_profile_id,"
        " embedding_profile_id, config_hash, started_at, finished_at, error"
        " FROM extraction_runs"
    )
    op.drop_table("extraction_runs")
    op.rename_table("extraction_runs_v1", "extraction_runs")
    op.create_index("ix_runs_book_status", "extraction_runs", ["book_id", "status"])
