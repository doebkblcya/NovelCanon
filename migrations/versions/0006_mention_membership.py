"""阶段 05 验收修复（第二轮）：mention 成员关系 + state_claims 真约束。

Revision ID: 0006_mention_membership
Revises: 0005_run_membership

背景（验收 P0/P1）：

- P0：checkpoint 复用关联产物时必须限定来源 run，否则同章其他 run（如失败
  run）的 staging claim 会随复用进入 active view。产物成员关系复制改为
  「按来源 run 的 observation 复制」，本轮迁移为 mention 补齐对称的
  mention_observations 表（entity_mentions.run_id 保持首次创建审计，
  不再被复用时整体改写）。

- P1：state_claims.subject_entity_id 需要真正的 NOT NULL + FOREIGN KEY
  （0005 触发器只在 subject IS NOT NULL 时校验实体存在，直接 SQL 仍可写
  NULL）。SQLite 无法对已有列增补 FK，按 12 步重建 state_claims；
  PRAGMA foreign_keys=ON 下删除被状态引用的实体同样被拒绝。
  重建后保留 0005 触发器作为双保险（foreign_keys 关闭时仍拦截）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_mention_membership"
down_revision = "0005_run_membership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── mention 成员关系（run_id 列不再承担「最近确认」语义）───
    op.create_table(
        "mention_observations",
        sa.Column(
            "mention_id",
            sa.Text,
            sa.ForeignKey("entity_mentions.mention_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "extraction_run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("mention_id", "extraction_run_id", name="pk_mention_observations"),
    )
    op.create_index("ix_mention_observations_run", "mention_observations", ["extraction_run_id"])
    op.execute(
        "INSERT OR IGNORE INTO mention_observations (mention_id, extraction_run_id,"
        " observed_at)"
        " SELECT mention_id, run_id, created_at FROM entity_mentions"
    )

    # ── state_claims 重建：subject_entity_id NOT NULL + FK entities ──
    # （0001 + 0004 的列 + subject 约束；触发器随旧表 drop 自动消失，稍后重建）
    op.create_table(
        "state_claims_new",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("field", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("raw_value", sa.Text, nullable=True),
        sa.Column(
            "subject_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=True,
        ),
    )
    op.execute(
        "INSERT INTO state_claims_new (claim_version_id, field, value, raw_value,"
        " subject_entity_id, target_entity_id)"
        " SELECT claim_version_id, field, value, raw_value, subject_entity_id,"
        " target_entity_id FROM state_claims"
    )
    op.drop_table("state_claims")
    op.rename_table("state_claims_new", "state_claims")
    op.create_index("ix_state_field", "state_claims", ["field", "value"])
    op.create_index("ix_state_subject", "state_claims", ["subject_entity_id", "field"])

    # ── 双保险触发器（真 FK 之外，foreign_keys=OFF 时仍拦截）────
    op.execute(
        "CREATE TRIGGER trg_state_subject_fk_insert "
        "BEFORE INSERT ON state_claims "
        "FOR EACH ROW "
        "WHEN NEW.subject_entity_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM entities WHERE canonical_id = NEW.subject_entity_id) "
        "BEGIN "
        "  SELECT RAISE(ABORT, 'state_claims.subject_entity_id 引用不存在的实体'); "
        "END"
    )
    op.execute(
        "CREATE TRIGGER trg_state_subject_fk_update "
        "BEFORE UPDATE OF subject_entity_id ON state_claims "
        "FOR EACH ROW "
        "WHEN NEW.subject_entity_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM entities WHERE canonical_id = NEW.subject_entity_id) "
        "BEGIN "
        "  SELECT RAISE(ABORT, 'state_claims.subject_entity_id 引用不存在的实体'); "
        "END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_state_subject_fk_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_state_subject_fk_update")

    # 还原 0005 形状：subject nullable（0004）+ 触发器
    op.create_table(
        "state_claims_old",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("field", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("raw_value", sa.Text, nullable=True),
        sa.Column("subject_entity_id", sa.Text, nullable=True),
        sa.Column(
            "target_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=True,
        ),
    )
    op.execute(
        "INSERT INTO state_claims_old (claim_version_id, field, value, raw_value,"
        " subject_entity_id, target_entity_id)"
        " SELECT claim_version_id, field, value, raw_value, subject_entity_id,"
        " target_entity_id FROM state_claims"
    )
    op.drop_table("state_claims")
    op.rename_table("state_claims_old", "state_claims")
    op.create_index("ix_state_field", "state_claims", ["field", "value"])
    op.create_index("ix_state_subject", "state_claims", ["subject_entity_id", "field"])
    op.execute(
        "CREATE TRIGGER trg_state_subject_fk_insert "
        "BEFORE INSERT ON state_claims "
        "FOR EACH ROW "
        "WHEN NEW.subject_entity_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM entities WHERE canonical_id = NEW.subject_entity_id) "
        "BEGIN "
        "  SELECT RAISE(ABORT, 'state_claims.subject_entity_id 引用不存在的实体'); "
        "END"
    )
    op.execute(
        "CREATE TRIGGER trg_state_subject_fk_update "
        "BEFORE UPDATE OF subject_entity_id ON state_claims "
        "FOR EACH ROW "
        "WHEN NEW.subject_entity_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM entities WHERE canonical_id = NEW.subject_entity_id) "
        "BEGIN "
        "  SELECT RAISE(ABORT, 'state_claims.subject_entity_id 引用不存在的实体'); "
        "END"
    )

    op.drop_table("mention_observations")
