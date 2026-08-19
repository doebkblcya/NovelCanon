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
- **supported ⇒ 关系证据硬约束**（验收 P0）：claim_status='supported' 的
  验证行必须同时携带 verification_method 与 verification_evidence
  （非空）——无关系证据的边不得进入默认因果回答；
- **回填来自 event_link_observations**（验收 P0）：成员关系才是多 run
  复用的真相——为每个 (link, run) 观察关系回填验证行（不能只按
  created_by_run_id），否则升级前复用该边的 active run 升级后会因
  INNER JOIN 丢失可见性；回填状态 = 全局行状态，但 supported 且缺
  方法/证据的存量降级为 unverified（满足硬约束）。
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
        # supported ⇒ 关系证据硬约束：方法 + 证据必须同时非空
        sa.CheckConstraint(
            "claim_status != 'supported' OR"
            " (verification_method IS NOT NULL"
            " AND verification_evidence IS NOT NULL)",
            name="ck_event_link_verification_supported",
        ),
    )
    # 回填：为每个 (link, run) 观察关系补验证行（成员关系是复用的真相）。
    # supported 且缺方法/证据的存量降级为 unverified（硬约束兜底）。
    op.execute(
        "INSERT OR IGNORE INTO event_link_verifications"
        " (claim_version_id, extraction_run_id, claim_status,"
        "  verification_method, verification_evidence, verified_at)"
        " SELECT o.claim_version_id, o.extraction_run_id,"
        "        CASE WHEN l.claim_status = 'supported'"
        "                  AND l.verification_method IS NOT NULL"
        "                  AND l.verification_evidence IS NOT NULL"
        "             THEN 'supported' ELSE 'unverified' END,"
        "        CASE WHEN l.claim_status = 'supported'"
        "                  AND l.verification_method IS NOT NULL"
        "                  AND l.verification_evidence IS NOT NULL"
        "             THEN l.verification_method ELSE NULL END,"
        "        CASE WHEN l.claim_status = 'supported'"
        "                  AND l.verification_method IS NOT NULL"
        "                  AND l.verification_evidence IS NOT NULL"
        "             THEN l.verification_evidence ELSE NULL END,"
        "        datetime('now')"
        " FROM event_link_observations o"
        " JOIN event_links l ON l.claim_version_id = o.claim_version_id"
    )


def downgrade() -> None:
    op.drop_table("event_link_verifications")
