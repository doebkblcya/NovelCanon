"""pytest 共享 fixture：迁移到最新版本的临时 SQLite 库。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from novelcanon.storage.engine import create_db_engine
from novelcanon.storage.repository import Repository

ALEMBIC_INI = "alembic.ini"


def migrate_to_head(db_path: object) -> None:
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


@pytest.fixture()
def migrated_db(tmp_path) -> Iterator[Engine]:
    """空库迁移到最新版本，返回可用 engine。"""
    db = tmp_path / "novelcanon_test.db"
    engine = create_db_engine(db)
    migrate_to_head(db)
    yield engine
    engine.dispose()


@pytest.fixture()
def repo(migrated_db: Engine) -> Repository:
    return Repository(migrated_db)
