"""阶段 09：event link 验证结果 run 作用域化（docs/implementation/09 §4）。

Revision ID: 0013_event_link_run_verification
Revises: 0012_event_link_world_valid

背景：验收 P0——event link 版本 ID 只由 fact/payload 决定，多 run 共享
同一条 event_links 行。此前幂等命中直接 UPDATE 全局行的 claim_status /
verification：**未激活 run 重新链接并验证失败时，会立即把旧 active run
同一边从 supported 降为 unverified**，激活前就改变了 active 查询结果。

本迁移把验证结果改为 **run 作用域**：
- event_link_verifications(claim_version_id, extraction_run_id,
  claim_status, verification_method, verification_evidence, verified_at)：
  每个 run 自己对每条边的验证结论；
- 查询（causal_paths / causal_results）经 active run 的 observation 关联
  该 run 的验证行——未激活 run 的验证不影响 active 查询；
- 回填：按 event_links.created_by_run_id + 全局 claim_status/verification
  补齐存量。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_event_link_run_verification"
down_revision = "0012_event_link_world_valid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_link_verifications",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("event_links.claim_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "extraction_run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("claim_status", sa.Text, nullable=False),
        sa.Column("verification_method", sa.Text, nullable=True),
        sa.Column("verification_evidence", sa.Text, nullable=True),
        sa.Column("verified_at", sa.Text, nullable=False),
    )
    # 回填：存量边按创建 run + 全局状态补齐
    op.execute(
        "INSERT OR IGNORE INTO event_link_verifications"
        " (claim_version_id, extraction_run_id, claim_status,"
        "  verification_method, verification_evidence, verified_at)"
        " SELECT claim_version_id, created_by_run_id, claim_status,"
        "        verification_method, verification_evidence, datetime('now')"
        " FROM event_links WHERE created_by_run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_table("event_link_verifications")
