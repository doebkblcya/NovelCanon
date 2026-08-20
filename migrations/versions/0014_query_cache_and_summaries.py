"""阶段 10：查询缓存与分层摘要产物表（docs/implementation/10 §5/§7）。

Revision ID: 0014_query_cache_and_summaries
Revises: 0013_event_link_run_verification

- query_cache：答案缓存。cache_key 为版本化键的 hash——键内包含
  book_id、标准化查询、query type、knowledge cutoff、world at chapter、
  active run 集合签名、active index version、query/synthesis profile；
  active run 或依赖版本变化 → 键变化 → 旧缓存自然不再命中（10 §5）。
- summary_artifacts：分层摘要产物（章节/卷/全书）。每个摘要保存
  输入 claim 版本集合、依赖的下级摘要版本、generation/profile/prompt
  版本、content hash、max observed ordinal；输入事实变化 → content hash
  变化 → 新 summary_id（新版本行），旧行标记 stale（10 §7 失效重建）。
- volumes 补 grouping_source：卷分组来源（source=原书卷标题 /
  default=每 50 章默认分组，10 §6）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_query_cache_and_summaries"
down_revision = "0013_event_link_run_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 查询缓存（10 §5）──────────────────────────────────────
    op.create_table(
        "query_cache",
        sa.Column("cache_key", sa.Text, primary_key=True),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column("normalized_query", sa.Text, nullable=False),
        sa.Column("query_type", sa.Text, nullable=False),
        sa.Column("knowledge_cutoff", sa.Integer, nullable=True),
        sa.Column("world_at", sa.Integer, nullable=True),
        # active run 集合（run_id + status）与 active index version 的签名；
        # 任一变化 → 签名变化 → 旧缓存不命中（10 §5）。
        sa.Column("active_run_signature", sa.Text, nullable=False),
        sa.Column(
            "index_version_id",
            sa.Text,
            sa.ForeignKey("index_versions.index_version_id"),
            nullable=True,
        ),
        sa.Column("query_profile", sa.Text, nullable=False, server_default=""),
        sa.Column("synthesis_profile", sa.Text, nullable=True),
        sa.Column("result", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_query_cache_book", "query_cache", ["book_id", "created_at"])

    # ── 分层摘要产物（10 §7）──────────────────────────────────
    op.create_table(
        "summary_artifacts",
        sa.Column("summary_id", sa.Text, primary_key=True),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column(
            "level",
            sa.Text,
            sa.CheckConstraint("level IN ('chapter','volume','book')"),
            nullable=False,
        ),
        sa.Column(
            "volume_id",
            sa.Text,
            sa.ForeignKey("volumes.volume_id"),
            nullable=True,
        ),
        sa.Column(
            "chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=True,
        ),
        # 卷级摘要所属的分组版本：分组重建后旧分组版本的摘要不再被引用
        sa.Column("grouping_version", sa.Text, nullable=True),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        # 输入 claim 版本集合（JSON 数组：claim_version_id）
        sa.Column("input_claim_versions", sa.Text, nullable=False, server_default="[]"),
        # 依赖的下级摘要版本（JSON 数组：summary_id）
        sa.Column("depends_on_summaries", sa.Text, nullable=False, server_default="[]"),
        sa.Column("generation_profile_id", sa.Text, nullable=True),
        sa.Column("prompt_version", sa.Text, nullable=False, server_default=""),
        sa.Column("schema_version", sa.Text, nullable=False, server_default=""),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("max_observed_ordinal", sa.Integer, nullable=False),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint("status IN ('valid','stale')"),
            nullable=False,
            server_default="valid",
        ),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_summaries_book_level", "summary_artifacts", ["book_id", "level", "status"]
    )
    op.create_index("ix_summaries_volume", "summary_artifacts", ["volume_id"])

    # ── 卷分组来源（10 §6）：原书卷标题 vs 每 50 章默认分组 ──────
    op.execute(
        "ALTER TABLE volumes ADD COLUMN grouping_source TEXT"
        " CHECK (grouping_source IN ('source','default'))"
    )


def downgrade() -> None:
    op.drop_index("ix_summaries_volume", table_name="summary_artifacts")
    op.drop_index("ix_summaries_book_level", table_name="summary_artifacts")
    op.drop_table("summary_artifacts")
    op.drop_index("ix_query_cache_book", table_name="query_cache")
    op.drop_table("query_cache")
    op.execute("ALTER TABLE volumes DROP COLUMN grouping_source")
