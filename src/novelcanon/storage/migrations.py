"""数据库迁移入口（ADR-0002：手写 revision，禁 autogenerate）。

提供与 cwd 无关的 migrate_to_head，供 CLI 与测试共用。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def migrate_to_head(db_path: Path) -> None:
    """把数据库迁移到最新版本（幂等，已在 head 时无操作）。"""
    command.upgrade(alembic_config(db_path), "head")
