"""阶段 05：state_claims 补 subject_entity_id（状态主体，查询必需）。

Revision ID: 0004_state_subject
Revises: 0003_pipeline
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_state_subject"
down_revision = "0003_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 注：SQLite ADD COLUMN 不能带 FK（alembic 不支持该约束变更）；
    # 实体引用完整性由 repository/应用层保证。
    op.add_column(
        "state_claims",
        sa.Column("subject_entity_id", sa.Text, nullable=True),
    )
    op.create_index("ix_state_subject", "state_claims", ["subject_entity_id", "field"])


def downgrade() -> None:
    op.drop_index("ix_state_subject", table_name="state_claims")
    op.drop_column("state_claims", "subject_entity_id")
