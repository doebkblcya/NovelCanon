"""阶段 11 复审：claim_evidence 允许同 span 多验证并存（run/version 成员关系）。

Revision ID: 0017_evidence_run_version
Revises: 0016_runs_abandoned

背景：P1（十五轮）——证据验证结果应具有 run/version 成员关系，同一
claim/span 在 v1、v2 规则或不同验证 run 下应并存（历史 run 可审计），
不得通过删除共享 evidence 完成升级。原 `uq_evidence_span` 唯一约束
(claim_version_id, chapter_id, char_start, char_end, span_hash) 禁止同一
span 保存多条验证——删除该约束，改为以 evidence_id（含 verification_version
+ verification_run_id，见 schemas/ids.py）为主键区分。

SQLite 不支持 ALTER DROP CONSTRAINT——按既有模式重建表。

P1（十六轮）：重建必须完整复制原表约束——evidence_stance / evidence_type
枚举 CHECK（ck_evidence_stance / ck_evidence_type）与 claim_version_id 外键
的 ON DELETE CASCADE（0001 定义），否则迁移后 schema 约束弱于原状
（非法枚举可写入、删除 claim 不再级联清理证据）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_evidence_run_version"
down_revision = "0016_runs_abandoned"
branch_labels = None
depends_on = None


def _columns() -> list[sa.Column]:
    """claim_evidence 完整列定义（含原 CHECK 与级联，upgrade/downgrade 共用）。"""
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


def upgrade() -> None:
    op.create_table("claim_evidence_v2", *_columns())
    op.execute(
        "INSERT INTO claim_evidence_v2 (evidence_id, claim_version_id, evidence_stance,"
        " evidence_type, chapter_id, char_start, char_end, span_hash, literal_match_rate,"
        " verification_method, verification_run_id)"
        " SELECT evidence_id, claim_version_id, evidence_stance, evidence_type, chapter_id,"
        " char_start, char_end, span_hash, literal_match_rate, verification_method,"
        " verification_run_id FROM claim_evidence"
    )
    op.drop_table("claim_evidence")
    op.rename_table("claim_evidence_v2", "claim_evidence")
    op.create_index("ix_evidence_chapter", "claim_evidence", ["chapter_id"])
    op.create_index(
        "ix_evidence_claim", "claim_evidence", ["claim_version_id", "verification_run_id"]
    )


def downgrade() -> None:
    # 还原 v1 表：与 0001 原定义一致（含枚举 CHECK 与级联删除），仅保留
    # 每 span 最新一条验证（0017 之前的唯一约束语义）。
    # SQLite 不支持 ALTER ADD CONSTRAINT——唯一约束必须在 CREATE TABLE 内联。
    op.create_table(
        "claim_evidence_v1",
        *_columns(),
        sa.UniqueConstraint(
            "claim_version_id",
            "chapter_id",
            "char_start",
            "char_end",
            "span_hash",
            name="uq_evidence_span",
        ),
    )
    # 还原时保留每个 span 最新一条（按 evidence_id 排序取最后）
    op.execute(
        "INSERT INTO claim_evidence_v1 (evidence_id, claim_version_id, evidence_stance,"
        " evidence_type, chapter_id, char_start, char_end, span_hash, literal_match_rate,"
        " verification_method, verification_run_id)"
        " SELECT evidence_id, claim_version_id, evidence_stance, evidence_type, chapter_id,"
        " char_start, char_end, span_hash, literal_match_rate, verification_method,"
        " verification_run_id"
        " FROM claim_evidence ce"
        " WHERE evidence_id = (SELECT MAX(e2.evidence_id) FROM claim_evidence e2"
        "                       WHERE e2.claim_version_id = ce.claim_version_id"
        "                         AND e2.chapter_id = ce.chapter_id"
        "                         AND e2.char_start = ce.char_start"
        "                         AND e2.char_end = ce.char_end"
        "                         AND e2.span_hash = ce.span_hash)"
    )
    op.drop_table("claim_evidence")
    op.rename_table("claim_evidence_v1", "claim_evidence")
    op.create_index("ix_evidence_chapter", "claim_evidence", ["chapter_id"])
