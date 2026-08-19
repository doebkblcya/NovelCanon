"""统一命令行入口（ADR-0004）。

CLI 只负责参数解析与调用 application service，不直接编写 SQL 或业务逻辑；
未实现的命令明确返回「尚未实现」，不得静默通过。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import structlog
import typer
from sqlalchemy import Engine

from novelcanon import __version__
from novelcanon.config.settings import AppSettings, GenerationProfile
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
    """导入原始书本并建立章节底座（阶段 03）。"""
    _log_command_invoked("import")
    from novelcanon.ingestion.service import import_book as run_import

    engine = _open_db()
    try:
        result = run_import(engine, path, book_id=book_id)
    finally:
        engine.dispose()
    typer.echo(
        f"✅ 已导入 book={result.book_id}「{result.title}」"
        f" 章节={result.chapter_count} 卷={result.volume_count} 格式={result.source_format}"
    )


@app.command()
def index(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
) -> None:
    """构建/重建原文索引（raw chunk、FTS、向量；阶段 03）。"""
    _log_command_invoked("index")
    from novelcanon.retrieval.indexer import build_index
    from novelcanon.retrieval.tokenizer import FakeTokenizer
    from novelcanon.retrieval.vectorstore import BruteForceVectorStore, FakeEmbedder

    engine = _open_db()
    try:
        result = build_index(
            engine,
            book_id,
            tokenizer=FakeTokenizer(),
            embedder=FakeEmbedder(dimension=8),
            vector_store=BruteForceVectorStore(dimension=8),
        )
    finally:
        engine.dispose()
    typer.echo(
        f"✅ 已建索引 index={result.index_version_id} chunks={result.chunk_count}"
        f" chunking={result.chunking_version[:12]}… 状态={result.status}"
    )


def _open_db() -> Engine:
    """打开已迁移到最新 schema 的数据库连接（SQLAlchemy Engine）。"""
    from novelcanon.storage.engine import create_db_engine
    from novelcanon.storage.migrations import migrate_to_head

    settings = AppSettings()
    migrate_to_head(settings.db_path)
    return create_db_engine(settings.db_path)


@app.command("extract")
def extract(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
    limit: Annotated[int | None, typer.Option(help="只处理前 N 章（开发用）")] = None,
    concurrency: Annotated[int, typer.Option(help="并发 worker 数")] = 4,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只验证配置与章节，不调用模型")
    ] = False,
) -> None:
    """逐章 Map 抽取 ExtractionDraftV1（阶段 06）。"""
    _log_command_invoked("extract")
    engine = _open_db()
    try:
        _run_extract(engine, book_id, limit=limit, concurrency=concurrency, dry_run=dry_run)
    finally:
        engine.dispose()


def _cli_generation_profile(concurrency: int) -> GenerationProfile:
    """从 AppSettings 构造 CLI generation profile（密钥只读环境，不落库）。

    LLM_* / NOVELCANON_LLM_* 环境变量与 .env（gitignore 已忽略）由
    pydantic-settings 统一加载；llm_api_key 字段 exclude=True，
    不进 config_hash / 日志 / 数据库。
    """
    settings = AppSettings()
    profile = GenerationProfile(
        profile_id="cli",
        context_window=settings.llm_context_window,
        max_output_tokens=settings.llm_max_output,
        structured_output_mode=settings.llm_mode,
        tokenizer_id=settings.llm_tokenizer,
        provider=settings.llm_provider,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key_env="LLM_API_KEY",
        concurrency_limit=max(1, concurrency),
    )
    if not settings.llm_model or not settings.llm_base_url:
        raise typer.BadParameter(
            "缺少模型配置：请设置 LLM_MODEL 与 LLM_BASE_URL"
            "（API key 用 LLM_API_KEY，本地 provider 可省略；均可写入 .env）"
        )
    return profile


def _tokenizer_for(profile: GenerationProfile):
    """按 profile.tokenizer_id 构造 tokenizer（fake-v1 / tiktoken-*）。"""
    from novelcanon.retrieval.tokenizer import FakeTokenizer, TiktokenAdapter

    if profile.tokenizer_id == "fake-v1" or profile.tokenizer_id.startswith("fake"):
        return FakeTokenizer()
    if profile.tokenizer_id.startswith("tiktoken-"):
        return TiktokenAdapter(encoding=profile.tokenizer_id.removeprefix("tiktoken-"))
    raise typer.BadParameter(f"未知 tokenizer_id：{profile.tokenizer_id}")


def _run_extract(
    engine: Engine,
    book_id: str,
    *,
    limit: int | None = None,
    concurrency: int = 4,
    dry_run: bool = False,
) -> None:
    """Map 流水线：真实 provider 按章产出 Draft 入 staging（run 保持 running）。"""
    import asyncio

    from novelcanon.config.hash import stable_config_hash
    from novelcanon.extraction.map_pipeline import build_map_process_fn
    from novelcanon.extraction.staging import MapStaging
    from novelcanon.generation import default_map_prompts
    from novelcanon.generation.client import GenerationClient
    from novelcanon.pipeline import (
        ChapterTask,
        PipelineRunner,
        RunManager,
        checkpoint_key,
    )
    from novelcanon.pipeline.checkpoint import CHECKPOINT_FIELDS
    from novelcanon.schemas.types import RunStatus
    from novelcanon.storage.repository import Repository

    profile = _cli_generation_profile(concurrency)
    prompts = default_map_prompts()
    repo = Repository(engine)
    chapters = repo.list_chapters(book_id)
    if limit is not None:
        chapters = chapters[:limit]
    if not chapters:
        typer.echo(f"❌ book={book_id} 没有章节，请先 novelcanon import")
        raise typer.Exit(1)

    tokenizer = _tokenizer_for(profile)
    typer.echo(
        f"Map 抽取 book={book_id} 章节={len(chapters)} profile={profile.profile_id}"
        f" model={profile.model or '（未配置）'} tokenizer={tokenizer.tokenizer_id}"
    )
    if dry_run:
        typer.echo("✅ dry-run：配置与章节校验通过，未调用模型")
        return

    settings = AppSettings()
    api_key = settings.llm_api_key or None
    if api_key is None and profile.api_key_env:
        typer.echo(f"⚠️  未设置 {profile.api_key_env}；若 provider 需要鉴权将失败")
    client = GenerationClient(profile, tokenizer=tokenizer, api_key=api_key)

    pipeline_version = "map-p1"
    schema_version = stable_config_hash(
        {"schema": prompts.schema_json, "profile": profile.config_hash}
    )
    prompt_version = prompts.version()

    tasks = [
        ChapterTask(
            chapter_id=ch["chapter_id"],
            ordinal=ch["ordinal"],
            content=repo.get_book_text(book_id)[ch["char_start"] : ch["char_end"]],
            checkpoint_fields={
                "book_id": book_id,
                "chapter_id": ch["chapter_id"],
                "content_hash": ch["content_hash"] or "",
                "pipeline_version": pipeline_version,
                "prompt_version": prompt_version,
                "compression_version": "",
                "schema_version": schema_version,
            },
        )
        for ch in chapters
    ]

    process_fn = build_map_process_fn(
        book_id=book_id,
        profile=profile,
        prompts=prompts,
        tokenizer=tokenizer,
        client=client,
    )

    mgr = RunManager(engine)
    run_id = mgr.create(
        book_id,
        input_hash=stable_config_hash(
            {
                "chapters": [
                    checkpoint_key({k: t.checkpoint_fields[k] for k in CHECKPOINT_FIELDS})
                    for t in tasks
                ]
            }
        ),
        pipeline_version=pipeline_version,
        prompt_version=prompt_version,
        schema_version=schema_version,
    )
    assert mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)

    runner = PipelineRunner(
        engine,
        run_id,
        book_id,
        concurrency=concurrency,
        staging=MapStaging(),
    )
    summary = asyncio.run(
        runner.run(
            tasks,
            process_fn,
            stage="map",
            timeout_seconds=profile.timeout_seconds + 5,
        )
    )
    from novelcanon.generation.report import extraction_report

    report = extraction_report(engine, run_id)
    typer.echo(
        f"✅ run={run_id} 完成：总={summary.total}"
        f" 新={summary.completed - summary.reused} 复用={summary.reused}"
        f" 失败={summary.failed}"
    )
    typer.echo(
        "   staging："
        f"valid={report['chapters'].get('valid', 0)}"
        f" invalid={report['chapters'].get('invalid', 0)}"
        f" failed={report['chapters'].get('failed', 0)}"
        f" claims={report['extraction']['claims']}"
        f" mentions={report['extraction']['mentions']}"
        f" unresolved={report['extraction']['unresolved']}"
        f" tokens={report['tokens'].get('total', 0)}"
    )
    typer.echo("   run 状态保持 running（阶段 07 证据验证后 activate）")


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
