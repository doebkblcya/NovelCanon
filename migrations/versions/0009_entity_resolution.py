"""阶段 08：实体消歧投影与 unresolved（docs/implementation/08）。

Revision ID: 0009_entity_resolution
Revises: 0008_evidence_errors

08 原则（§基本原则 + §6）：
- canonical_id 不依赖名称/ordinal/run：UUID，跨 run 经 alias claim 复用；
- 下游 claim 通过映射重写，不直接修改不可审计历史：entity_resolutions
  是 mention → canonical 的投影表（可重放/重建 canonical 投影）；
- unresolved 是正式流水线产物：unresolved_mentions 落库并统计；
- merge/split 不物理覆盖历史：entity_merge_audit（0001 已建）记录
  来源/目标/理由/run，本迁移不改动。

投影表说明：
- entity_resolutions(mention_id, canonical_id, resolver_version, reason,
  run_id)：mention 的 canonical 归属，可重放；查询层据此把
  canonical 展开为自身 + 全部 mention（claim 引用保持 mention_id，
  不改写历史）；
- unresolved_mentions(surface_name, chapter_id, char_start, char_end,
  context, reason, run_id)：泛称/低置信 mention，统计与人工标注队列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_entity_resolution"
down_revision = "0008_evidence_errors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entity_resolutions",
        sa.Column(
            "mention_id",
            sa.Text,
            sa.ForeignKey("entity_mentions.mention_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "canonical_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=False,
        ),
        sa.Column("resolver_version", sa.Text, nullable=False, server_default=""),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_entity_resolutions_canonical", "entity_resolutions", ["canonical_id"]
    )
    op.create_index(
        "ix_entity_resolutions_run", "entity_resolutions", ["run_id"]
    )

    op.create_table(
        "unresolved_mentions",
        sa.Column("unresolved_id", sa.Text, primary_key=True),
        sa.Column("surface_name", sa.Text, nullable=False),
        sa.Column(
            "chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=False,
        ),
        sa.Column("char_start", sa.Integer, nullable=False, server_default="0"),
        sa.Column("char_end", sa.Integer, nullable=False, server_default="0"),
        sa.Column("context", sa.Text, nullable=False, server_default=""),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint(
            "surface_name", "chapter_id", name="uq_unresolved_mention"
        ),
    )
    op.create_index(
        "ix_unresolved_run", "unresolved_mentions", ["run_id"]
    )


def downgrade() -> None:
    op.drop_table("unresolved_mentions")
    op.drop_table("entity_resolutions")
