"""结构化日志：JSON/控制台输出与运行上下文绑定（ADR-0005）。

约束（定版方案 §01.4）：不记录完整原文、模型密钥或无上限的模型响应；
每条流水线日志包含 book_id / run_id / chapter_id（适用时）。
"""

from __future__ import annotations

import logging

import structlog
from structlog.typing import Processor


def configure_logging(*, level: str = "INFO", log_json: bool = False) -> None:
    """初始化 structlog 与 stdlib logging。

    ``log_json=True`` 输出 JSON 行（生产/CI 友好），否则输出可读控制台格式。
    """
    level = level.upper()
    level_int = logging.getLevelNamesMapping()[level]
    logging.basicConfig(level=level_int, format="%(message)s")

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer() if log_json else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_run_context(
    *,
    book_id: str | None = None,
    run_id: str | None = None,
    chapter_id: str | None = None,
    command: str | None = None,
) -> None:
    """绑定流水线/命令日志上下文；未提供的字段不写入。"""
    structlog.contextvars.bind_contextvars(
        book_id=book_id, run_id=run_id, chapter_id=chapter_id, command=command
    )
