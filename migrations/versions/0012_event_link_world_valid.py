"""阶段 09：event_links 世界有效时间列（docs/implementation/09 §7）。

Revision ID: 0012_event_link_world_valid
Revises: 0011_event_link_verification

背景：定版方案要求关系、状态、势力和图谱边都按 world_valid 过滤。
EventLink 此前没有 world_valid 列（默认 unknown），causal_paths 也没有
world_at 参数。本迁移：

- event_links 补 world_valid_kind / world_valid_from / world_valid_to /
  world_valid_confidence（与 claims 同构）；
- **数据约束（验收 P1/P2）**：world_valid_kind NOT NULL DEFAULT
  'unknown'（每边具有明确时间类型，弱于/对齐 claims 表），kind 枚举
  （story_time/chapter_proxy/unknown）、confidence 0–1、组合约束
  （kind 非 unknown 时必须有 from），全部由 SQLite CHECK 兜底；
- 回填：存量边按 chapter_proxy 语义（world_valid_from = observed_ordinal，
  事件在披露章节发生）补齐，其余明确为 unknown，立即可查询。

注：SQLite 的 ALTER TABLE ADD COLUMN 支持内联 NOT NULL DEFAULT 与 CHECK
（对新写入强制，存量行填默认值）；跨列组合约束挂在 from 列上引用
已存在的 kind 列。
"""

from __future__ import annotations

from alembic import op

revision = "0012_event_link_world_valid"
down_revision = "0011_event_link_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # kind 每边必填（默认 unknown），枚举由 CHECK 兜底
    op.execute(
        "ALTER TABLE event_links ADD COLUMN world_valid_kind TEXT"
        " NOT NULL DEFAULT 'unknown'"
        " CHECK (world_valid_kind IN ('story_time','chapter_proxy','unknown'))"
    )
    # 组合约束：kind 非 unknown 时必须给出 world_valid_from
    op.execute(
        "ALTER TABLE event_links ADD COLUMN world_valid_from INTEGER"
        " CHECK (world_valid_from IS NOT NULL OR world_valid_kind = 'unknown')"
    )
    op.execute("ALTER TABLE event_links ADD COLUMN world_valid_to INTEGER")
    op.execute(
        "ALTER TABLE event_links ADD COLUMN world_valid_confidence REAL"
        " CHECK (world_valid_confidence IS NULL OR"
        " (world_valid_confidence >= 0 AND world_valid_confidence <= 1))"
    )
    # 回填：存量边按 chapter_proxy（world_valid_from = 披露章节）补齐，
    # 无披露章节的边明确为 unknown（每边都有明确时间类型）
    op.execute(
        "UPDATE event_links SET world_valid_kind = 'chapter_proxy',"
        " world_valid_from = observed_ordinal, world_valid_to = NULL,"
        " world_valid_confidence = 1.0"
        " WHERE world_valid_kind = 'unknown' AND observed_ordinal IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("event_links", "world_valid_confidence")
    op.drop_column("event_links", "world_valid_to")
    op.drop_column("event_links", "world_valid_from")
    op.drop_column("event_links", "world_valid_kind")
