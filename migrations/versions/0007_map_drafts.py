"""阶段 06：Map 产物 staging 表（ExtractionDraftV1 落库载体）。

Revision ID: 0007_map_drafts
Revises: 0006_mention_membership

背景：Map 只输出本章可确定信息（mention / local_event / provisional_claim /
ref_source_segment / local_causes / cause_candidates / unresolved），
不生成 canonical_id 与最终证据坐标（ref_source_segment → 原文 span 对齐
属阶段 07 证据验证）。map_drafts 是 Map 产物与校验元数据的 staging 载体：

- 每 run 每章一行（UNIQUE(run_id, chapter_id)），同 run 重复投递幂等；
- draft_id 为确定性 ID（book/chapter/内容/版本，run 不进 ID）；
- status: valid / invalid / failed —— 无效输出保存错误摘要与响应 hash，
  完整原始响应按安全配置保存在 raw_response（不进入正式事实表）；
- validation_issues 保存结构化校验问题（按 code 统计抽取报告）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_map_drafts"
down_revision = "0006_mention_membership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PK = (run_id, chapter_id)：每 run 每章一行；draft_id 为确定性内容键
    # （book/chapter/内容/版本，run 不进 ID），跨 run 相同，不能作 PK。
    op.create_table(
        "map_drafts",
        sa.Column("draft_id", sa.Text, nullable=False),
        sa.Column(
            "run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
        sa.Column(
            "book_id",
            sa.Text,
            sa.ForeignKey("books.book_id"),
            nullable=False,
        ),
        sa.Column(
            "chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False, server_default=""),
        sa.Column("schema_version", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint(
                "status IN ('valid','invalid','failed')", name="ck_map_drafts_status"
            ),
            nullable=False,
            server_default="valid",
        ),
        sa.Column("draft_json", sa.Text, nullable=True),
        sa.Column("request_hash", sa.Text, nullable=False, server_default=""),
        sa.Column("response_hash", sa.Text, nullable=False, server_default=""),
        sa.Column("validation_issues", sa.Text, nullable=False, server_default="[]"),
        sa.Column("error_summary", sa.Text, nullable=True),
        sa.Column("raw_response", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("run_id", "chapter_id", name="pk_map_drafts_run_chapter"),
    )
    op.create_index("ix_map_drafts_draft_id", "map_drafts", ["draft_id"])
    op.create_index("ix_map_drafts_run", "map_drafts", ["run_id"])
    op.create_index("ix_map_drafts_chapter", "map_drafts", ["book_id", "chapter_id"])


def downgrade() -> None:
    op.drop_table("map_drafts")
