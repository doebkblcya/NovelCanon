"""Alembic 迁移环境。

约束（ADR-0002）：migration 全部手写 revision，禁用 autogenerate——
FTS5/vec 虚表、trigger、partial 索引、表重建式迁移均不依赖 autogenerate。
数据库 URL 从应用配置读取（NOVELCANON_DB_PATH 环境变量可覆盖）。
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from novelcanon.config.settings import AppSettings

config = context.config

if not config.get_main_option("sqlalchemy.url"):
    settings = AppSettings()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{settings.db_path}")


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本，不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, render_as_batch=False)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
