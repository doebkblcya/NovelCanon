"""阶段 07：证据对齐错误表（docs/implementation/07 §产物）。

Revision ID: 0008_evidence_errors
Revises: 0007_map_drafts

证据处理链（07 §证据处理链）：
ref_source_segment -> 原文候选范围 -> 字面匹配和 span 候选 -> 验证
-> claim_evidence -> claim_status 聚合。

任一级（ref 回映射 / span 候选 / 验证）hash 或范围失败都必须进入
staging/error，不能猜测修复后直接激活（07 §1）。evidence_errors 是
对齐失败的审计载体：

- 记录失败发生在哪一级（stage: ref_mapping / span_candidate / verification）；
- 记录失败的具体 claim 与 ref 段（可追溯到 ref、候选选择和验证方法，退出标准）；
- error_id 确定性（run/chapter/claim/stage/code），重跑验证不产生重复错误。

对齐成功路径（claim_evidence + claims.primary_evidence_id）沿用既有表，
不在本迁移重复建表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_evidence_errors"
down_revision = "0007_map_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_errors",
        sa.Column("error_id", sa.Text, primary_key=True),
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
        sa.Column("claim_id", sa.Text, nullable=False, server_default=""),
        sa.Column("ref_segment_id", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "stage",
            sa.Text,
            sa.CheckConstraint(
                "stage IN ('ref_mapping','span_candidate','verification')",
                name="ck_evidence_errors_stage",
            ),
            nullable=False,
        ),
        sa.Column(
            "error_code",
            sa.Text,
            sa.CheckConstraint(
                "error_code IN ('ref_hash_mismatch','ref_out_of_range',"
                "'ref_missing','no_span_found','verification_failed')",
                name="ck_evidence_errors_code",
            ),
            nullable=False,
        ),
        sa.Column("message", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "chapter_id",
            "claim_id",
            "stage",
            "error_code",
            name="uq_evidence_errors_identity",
        ),
    )
    op.create_index("ix_evidence_errors_run", "evidence_errors", ["run_id"])
    op.create_index(
        "ix_evidence_errors_chapter", "evidence_errors", ["book_id", "chapter_id"]
    )


def downgrade() -> None:
    op.drop_table("evidence_errors")
