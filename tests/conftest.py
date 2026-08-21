"""pytest 共享 fixture：迁移到最新版本的临时 SQLite 库 + fixture EPUB。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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


@pytest.fixture()
def epub_file(tmp_path) -> Path:
    """确定性 fixture EPUB（3 章，含黄金专名/原句）。"""
    from tests.helpers import FIXTURE_CHAPTERS, make_fixture_epub

    path = tmp_path / "fixture.epub"
    make_fixture_epub(path, FIXTURE_CHAPTERS)
    return path


@pytest.fixture()
def imported_book(migrated_db: Engine, epub_file: Path) -> tuple[Engine, str]:
    """已导入 fixture EPUB 的 (engine, book_id)。"""
    from novelcanon.ingestion.service import import_book

    result = import_book(migrated_db, epub_file)
    return migrated_db, result.book_id


@pytest.fixture(autouse=True)
def _no_llm_in_tests(monkeypatch):
    """测试会话强制无 LLM 配置（复审 P1：离线、确定性、不产生费用）。

    真实 .env 可能配置了 NOVELCANON_LLM_*——若不禁用，create_app /
    CLI 会构造生产合成 client 并真实调用模型（完整测试出现 408、依赖
    外部服务）。空字符串覆盖 .env（BaseSettings 中 os.environ 优先）。
    """
    monkeypatch.setenv("NOVELCANON_LLM_MODEL", "")
    monkeypatch.setenv("NOVELCANON_LLM_BASE_URL", "")
    monkeypatch.setenv("NOVELCANON_LLM_API_KEY", "")
