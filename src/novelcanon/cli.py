"""统一命令行入口（ADR-0004）。

CLI 只负责参数解析与调用 application service，不直接编写 SQL 或业务逻辑；
未实现的命令明确返回「尚未实现」，不得静默通过。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import structlog
import typer

from novelcanon import __version__
from novelcanon.config.settings import AppSettings
from novelcanon.logging import bind_run_context, configure_logging

app = typer.Typer(
    name="novelcanon",
    help="将中文长篇小说转换为可追溯、可按章节查询的结构化知识库。",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"novelcanon {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="显示版本并退出",
        ),
    ] = None,
    log_level: Annotated[
        str, typer.Option("--log-level", help="日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）")
    ] = "INFO",
    log_json: Annotated[bool, typer.Option("--log-json", help="JSON 日志输出")] = False,
) -> None:
    """NovelCanon 统一 CLI。"""
    # 启动强校验（ADR-0003）：无效配置（含 NOVELCANON_* 环境变量）立即失败
    # 并给出明确字段错误，不允许静默使用不完整默认值。
    AppSettings()
    configure_logging(level=log_level.upper(), log_json=log_json)


def _log_command_invoked(command: str) -> None:
    """记录一次命令执行（01 验证项：日志关联命令执行，且不泄露敏感信息）。"""
    bind_run_context(command=command)
    structlog.get_logger("novelcanon.cli").info("command_invoked", version=__version__)


@app.command("import")
def import_book(
    path: Annotated[Path, typer.Argument(help="原始书本文件（EPUB/TXT）路径")],
    book_id: Annotated[str | None, typer.Option(help="指定稳定 book_id（缺省自动分配）")] = None,
) -> None:
    """导入原始书本并建立章节底座（阶段 03 实现）。"""
    _log_command_invoked("import")
    typer.echo(f"尚未实现：import（book={book_id or 'auto'}，path={path}）")


@app.command()
def index() -> None:
    """构建/重建原文索引（raw chunk、FTS、向量；阶段 03 实现）。"""
    _log_command_invoked("index")
    typer.echo("尚未实现：index（阶段 03 原文导入与索引底座）")


@app.command()
def extract() -> None:
    """逐章 Map 抽取 ExtractionDraftV1（阶段 06 实现）。"""
    _log_command_invoked("extract")
    typer.echo("尚未实现：extract（阶段 06 逐章 Map 抽取）")


@app.command()
def activate() -> None:
    """原子激活已完成验证的 run（阶段 04 实现）。"""
    _log_command_invoked("activate")
    typer.echo("尚未实现：activate（阶段 04 可恢复流水线）")


@app.command()
def query() -> None:
    """结构化查询 / 混合检索 / 问答（阶段 10 实现）。"""
    _log_command_invoked("query")
    typer.echo("尚未实现：query（阶段 10 查询检索与分层摘要）")


@app.command()
def inspect() -> None:
    """检查库内对象（book/run/chapter 等；阶段 02 起逐步实现）。"""
    _log_command_invoked("inspect")
    typer.echo("尚未实现：inspect（阶段 02 数据契约与存储）")


if __name__ == "__main__":
    app()
