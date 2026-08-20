"""阶段 10 复审：token_ledger.run_id 可空（查询/摘要记账，P1）。

Revision ID: 0015_ledger_run_optional
Revises: 0014_query_cache_and_summaries

背景：验收 P1——LLM 问答与 LLM Reduce 的 token 计量此前只汇总在内存
（RouteStats/SummaryResult），没有持久化账本。查询/摘要调用不绑定
extraction run，token_ledger.run_id 此前 NOT NULL 无法记账；本迁移把
run_id 改为可空（book_id 仍必填），使查询（stage='query'）与摘要
（stage='summary'）也能入账。SQLite 不支持 ALTER DROP NOT NULL，
按 0003 的方式重建表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_ledger_run_optional"
down_revision = "0014_query_cache_and_summaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "token_ledger_v2",
        sa.Column("ledger_id", sa.Integer, sa.Identity(), primary_key=True),
        # run_id 可空：查询/摘要类调用无 run 归属
        sa.Column(
            "run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=True,
        ),
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
    op.execute(
        "INSERT INTO token_ledger_v2 (ledger_id, run_id, book_id, chapter_id,"
        " stage, provider, model, profile_id, input_tokens, cached_input_tokens,"
        " reasoning_tokens, output_tokens, retry_count, discarded_tokens, created_at)"
        " SELECT ledger_id, run_id, book_id, chapter_id, stage, provider, model,"
        " profile_id, input_tokens, cached_input_tokens, reasoning_tokens,"
        " output_tokens, retry_count, discarded_tokens, created_at"
        " FROM token_ledger"
    )
    op.drop_table("token_ledger")
    op.rename_table("token_ledger_v2", "token_ledger")
    op.create_index("ix_ledger_run_stage", "token_ledger", ["run_id", "stage"])


def downgrade() -> None:
    op.create_table(
        "token_ledger_v1",
        sa.Column("ledger_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
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
    op.execute(
        "INSERT INTO token_ledger_v1 (ledger_id, run_id, book_id, chapter_id,"
        " stage, provider, model, profile_id, input_tokens, cached_input_tokens,"
        " reasoning_tokens, output_tokens, retry_count, discarded_tokens, created_at)"
        " SELECT ledger_id, run_id, book_id, chapter_id, stage, provider, model,"
        " profile_id, input_tokens, cached_input_tokens, reasoning_tokens,"
        " output_tokens, retry_count, discarded_tokens, created_at"
        " FROM token_ledger"
        " WHERE run_id IS NOT NULL"
    )
    op.drop_table("token_ledger")
    op.rename_table("token_ledger_v1", "token_ledger")
    op.create_index("ix_ledger_run_stage", "token_ledger", ["run_id", "stage"])
