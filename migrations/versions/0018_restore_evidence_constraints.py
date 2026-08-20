"""阶段 11 复审：恢复 claim_evidence 枚举 CHECK 与级联删除（十六轮 P1）。

Revision ID: 0018_restore_evidence_constraints
Revises: 0017_evidence_run_version

背景：0017 重建 claim_evidence 时丢失原约束（evidence_stance/evidence_type
枚举 CHECK、claim_version_id FK 的 ON DELETE CASCADE）。十六轮已在 0017
文件内补回，但已应用 0017 的数据库不会重放同一迁移——本迁移对**已标记
0017 的库**强制重建，恢复约束并**原样迁移全部证据行**（不按 span 去重：
0017 语义允许同 span 多验证并存，v1/v2 与跨 run 历史必须完整保留）。

禁止在真实库 downgrade 到 0016 再升级：0017 的 downgrade 按 span 取
MAX(evidence_id) 去重，会丢失 v1/v2 并存历史。本迁移是唯一安全路径。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_restore_evidence_constraints"
down_revision = "0017_evidence_run_version"
branch_labels = None
depends_on = None

_EVIDENCE_COLUMNS = (
    "evidence_id, claim_version_id, evidence_stance, evidence_type, chapter_id,"
    " char_start, char_end, span_hash, literal_match_rate, verification_method,"
    " verification_run_id"
)


def _constrained_columns() -> list[sa.Column]:
    """带完整原约束的列定义（evidence_stance/evidence_type CHECK + 级联）。"""
    return [
        sa.Column("evidence_id", sa.Text, primary_key=True),
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evidence_stance",
            sa.Text,
            sa.CheckConstraint(
                "evidence_stance IN ('supports','refutes','unclear')",
                name="ck_evidence_stance",
            ),
            nullable=False,
            server_default="supports",
        ),
        sa.Column(
            "evidence_type",
            sa.Text,
            sa.CheckConstraint(
                "evidence_type IN ('direct','contextual','inferred')",
                name="ck_evidence_type",
            ),
            nullable=False,
            server_default="direct",
        ),
        sa.Column(
            "chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=False,
        ),
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("span_hash", sa.Text, nullable=False),
        sa.Column(
            "literal_match_rate",
            sa.REAL,
            sa.CheckConstraint("literal_match_rate >= 0 AND literal_match_rate <= 1"),
            nullable=False,
            server_default="0",
        ),
        sa.Column("verification_method", sa.Text, nullable=False, server_default=""),
        sa.Column("verification_run_id", sa.Text, nullable=True),
    ]


def _unconstrained_columns() -> list[sa.Column]:
    """0017 upgrade 时的结构（无枚举 CHECK / 无级联，供 downgrade 还原）。"""
    return [
        sa.Column("evidence_id", sa.Text, primary_key=True),
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id"),
            nullable=False,
        ),
        sa.Column("evidence_stance", sa.Text, nullable=False, server_default="supports"),
        sa.Column("evidence_type", sa.Text, nullable=False, server_default="direct"),
        sa.Column(
            "chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=False,
        ),
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("span_hash", sa.Text, nullable=False),
        sa.Column(
            "literal_match_rate",
            sa.REAL,
            sa.CheckConstraint("literal_match_rate >= 0 AND literal_match_rate <= 1"),
            nullable=False,
            server_default="0",
        ),
        sa.Column("verification_method", sa.Text, nullable=False, server_default=""),
        sa.Column("verification_run_id", sa.Text, nullable=True),
    ]


def _copy_all(src: str, dst: str) -> None:
    """原样迁移全部行（不按 span 去重，保留 v1/v2 与跨 run 并存）。"""
    op.execute(f"INSERT INTO {dst} ({_EVIDENCE_COLUMNS}) SELECT {_EVIDENCE_COLUMNS} FROM {src}")


def upgrade() -> None:
    op.create_table("claim_evidence_restored", *_constrained_columns())
    _copy_all("claim_evidence", "claim_evidence_restored")
    op.drop_table("claim_evidence")
    op.rename_table("claim_evidence_restored", "claim_evidence")
    op.create_index("ix_evidence_chapter", "claim_evidence", ["chapter_id"])
    op.create_index(
        "ix_evidence_claim", "claim_evidence", ["claim_version_id", "verification_run_id"]
    )


def downgrade() -> None:
    # 回 0017 upgrade 后的结构（无枚举 CHECK / 无级联），数据原样保留
    op.create_table("claim_evidence_0017", *_unconstrained_columns())
    _copy_all("claim_evidence", "claim_evidence_0017")
    op.drop_table("claim_evidence")
    op.rename_table("claim_evidence_0017", "claim_evidence")
    op.create_index("ix_evidence_chapter", "claim_evidence", ["chapter_id"])
    op.create_index(
        "ix_evidence_claim", "claim_evidence", ["claim_version_id", "verification_run_id"]
    )
