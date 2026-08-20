"""阶段 05 验收修复：run 产物成员关系 + active 视图重建 + 状态主体约束。

Revision ID: 0005_run_membership
Revises: 0004_state_subject

背景（验收 P0）：v_active_claims 原先按 claims.created_by_run_id 判断可见性；
新 run 幂等复用既有版本并激活后，旧 run 被 supersede，复用 claim 从视图消失。
修复：可见性改为「当前 active run 的产物成员关系」推导：

- claim 成员关系复用 claim_observations（§4.3：某 run 观察到该版本，
  INSERT OR IGNORE，天然幂等）；
- 新增 alias_observations，与 claim_observations 同构（别名同样需要
  run-membership，display_name 依赖）；
- checkpoint 复用时由 PipelineRunner 把该章产物重新关联到当前 run；
- v_active_claims 按 claim_observations JOIN active run 推导，并带 book_id 列
  （多书隔离，P1）；
- state_claims.subject_entity_id 补实体引用完整性触发器（SQLite 不支持
  对已有列 ADD FK，用触发器实现等价约束，P1）。

回填：迁移前所有 claim/alias 按其首次创建 run 补成员关系，旧数据立即可见。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_run_membership"
down_revision = "0004_state_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── alias 成员关系（与 claim_observations 同构）────────────
    op.create_table(
        "alias_observations",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("entity_alias_claims.claim_version_id", ondelete="CASCADE"),
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
            "claim_version_id", "extraction_run_id", name="pk_alias_observations"
        ),
    )
    op.create_index("ix_alias_observations_run", "alias_observations", ["extraction_run_id"])

    # ── 回填成员关系（迁移前数据按首次创建 run 可见）──────────
    op.execute(
        "INSERT OR IGNORE INTO claim_observations (claim_version_id, extraction_run_id,"
        " observed_at)"
        " SELECT claim_version_id, created_by_run_id, created_at FROM claims"
    )
    op.execute(
        "INSERT OR IGNORE INTO alias_observations (claim_version_id, extraction_run_id,"
        " observed_at)"
        " SELECT claim_version_id, created_by_run_id, created_at FROM entity_alias_claims"
    )

    # ── 重建 active 视图：成员关系 + active run + book_id 列 ──
    op.execute("DROP VIEW IF EXISTS v_active_claims")
    op.execute(
        "CREATE VIEW v_active_claims AS "
        "SELECT c.rowid AS _rowid, c.*, r.book_id AS book_id "
        "FROM claims c "
        "JOIN claim_observations o ON o.claim_version_id = c.claim_version_id "
        "JOIN extraction_runs r ON r.run_id = o.extraction_run_id "
        "WHERE r.status = 'active'"
    )

    # ── 状态主体实体引用完整性（等价 FK 的触发器约束）──────────
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
    op.execute("DROP VIEW IF EXISTS v_active_claims")
    # 还原 0003 的视图定义（按 created_by_run_id 判断可见性）
    op.execute(
        "CREATE VIEW v_active_claims AS "
        "SELECT c.rowid AS _rowid, c.* FROM claims c "
        "JOIN extraction_runs r ON c.created_by_run_id = r.run_id "
        "WHERE r.status = 'active'"
    )
    op.drop_table("alias_observations")
