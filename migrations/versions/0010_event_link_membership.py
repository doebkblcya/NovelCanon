"""阶段 09：event_links 成员关系与创建 run（docs/implementation/09 §4）。

Revision ID: 0010_event_link_membership
Revises: 0009_entity_resolution

背景：event_links（0001 建表）没有 created_by_run_id 列，也没有
observation 成员关系表。阶段 09 因果递归查询需要按 run 可见性过滤
（与 claims/aliases 同构，验收 P0 教训：可见性必须走成员关系而非
created_by 单列）。

- event_links 补 created_by_run_id（幂等落库与审计）；
- event_link_observations（claim_version_id, extraction_run_id）成员关系，
  幂等复用（同 fact 重复链接只增 observation，不重复写边）；
- 回填：按创建 run 补成员关系（迁移前数据立即可见）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_event_link_membership"
down_revision = "0009_entity_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_links",
        sa.Column("created_by_run_id", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_event_links_run", "event_links", ["created_by_run_id"]
    )

    op.create_table(
        "event_link_observations",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("event_links.claim_version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "extraction_run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint(
            "claim_version_id",
            "extraction_run_id",
            name="pk_event_link_observations",
        ),
    )
    op.create_index(
        "ix_event_link_observations_run",
        "event_link_observations",
        ["extraction_run_id"],
    )
    # 回填：既有 event_links 按 created_by_run_id 补成员关系
    op.execute(
        "UPDATE event_links SET created_by_run_id ="
        " (SELECT created_by_run_id FROM claims"
        "  WHERE claims.claim_version_id = event_links.claim_version_id)"
        " WHERE created_by_run_id IS NULL"
    )
    op.execute(
        "INSERT OR IGNORE INTO event_link_observations (claim_version_id,"
        " extraction_run_id, observed_at)"
        " SELECT claim_version_id, created_by_run_id,"
        " COALESCE((SELECT created_at FROM claims"
        "           WHERE claims.claim_version_id = event_links.claim_version_id),"
        "          '1970-01-01T00:00:00+00:00')"
        " FROM event_links WHERE created_by_run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_table("event_link_observations")
    op.drop_index("ix_event_links_run", table_name="event_links")
    op.drop_column("event_links", "created_by_run_id")
