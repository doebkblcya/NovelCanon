"""阶段 09：event_links 世界有效时间列（docs/implementation/09 §7）。

Revision ID: 0012_event_link_world_valid
Revises: 0011_event_link_verification

背景：定版方案要求关系、状态、势力和图谱边都按 world_valid 过滤。
EventLink 此前没有 world_valid 列（默认 unknown），causal_paths 也没有
world_at 参数。本迁移：

- event_links 补 world_valid_kind / world_valid_from / world_valid_to /
  world_valid_confidence（与 claims 同构）；
- 回填：存量边按 chapter_proxy 语义（world_valid_from = observed_ordinal，
  事件在披露章节发生）补齐，立即可查询。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_event_link_world_valid"
down_revision = "0011_event_link_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_links",
        sa.Column("world_valid_kind", sa.Text, nullable=True),
    )
    op.add_column(
        "event_links",
        sa.Column("world_valid_from", sa.Integer, nullable=True),
    )
    op.add_column(
        "event_links",
        sa.Column("world_valid_to", sa.Integer, nullable=True),
    )
    op.add_column(
        "event_links",
        sa.Column("world_valid_confidence", sa.REAL, nullable=True),
    )
    # 回填：存量边按 chapter_proxy（world_valid_from = 披露章节）补齐
    op.execute(
        "UPDATE event_links SET world_valid_kind = 'chapter_proxy',"
        " world_valid_from = observed_ordinal, world_valid_to = NULL,"
        " world_valid_confidence = 1.0"
        " WHERE world_valid_kind IS NULL AND observed_ordinal IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("event_links", "world_valid_confidence")
    op.drop_column("event_links", "world_valid_to")
    op.drop_column("event_links", "world_valid_from")
    op.drop_column("event_links", "world_valid_kind")
