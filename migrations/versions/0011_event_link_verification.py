"""阶段 09：event_links 关系证据验证列（docs/implementation/09 §4）。

Revision ID: 0011_event_link_verification
Revises: 0010_event_link_membership

背景：验收 P0——规则层只生成 candidate，边要 supported 必须有**关系
证据**（目标章原文同时出现原因引用 + 因果连接词，LinkVerifier 验证）。
验证结果需要落库可审计：

- verification_method：验证方法（causal-connective 等），NULL = 未验证；
- verification_evidence：验证证据原文 span 的 JSON（chapter_id /
  char_start / char_end / span_text / matched_ref / matched_connective）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_event_link_verification"
down_revision = "0010_event_link_membership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_links",
        sa.Column("verification_method", sa.Text, nullable=True),
    )
    op.add_column(
        "event_links",
        sa.Column("verification_evidence", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("event_links", "verification_evidence")
    op.drop_column("event_links", "verification_method")
