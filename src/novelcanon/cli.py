"""统一命令行入口（ADR-0004）。

CLI 只负责参数解析与调用 application service，不直接编写 SQL 或业务逻辑；
未实现的命令明确返回「尚未实现」，不得静默通过。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import structlog
import typer
from sqlalchemy import Engine, text

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
    from novelcanon.retrieval.factory import create_backend, register_configured_backends
    from novelcanon.retrieval.indexer import build_index
    from novelcanon.retrieval.tokenizer import FakeTokenizer
    from novelcanon.retrieval.vectorstore import BruteForceVectorStore, FakeEmbedder

    settings = AppSettings()
    engine = _open_db()
    embedder = None
    try:
        if settings.embedding_profile_id:
            # 生产 embedding（NOVELCANON_EMBEDDING_* 配置，阶段 11 复审 D）：
            # 经 factory 注册表创建真实 adapter，索引 profile 与查询后端一致
            register_configured_backends(settings)
            embedder, vector_store = create_backend(settings.embedding_profile_id)
            label = settings.embedding_profile_id
        else:
            embedder, vector_store = FakeEmbedder(dimension=8), BruteForceVectorStore(dimension=8)
            label = "fake-embed-v8"
        result = build_index(
            engine,
            book_id,
            tokenizer=FakeTokenizer(),
            embedder=embedder,
            vector_store=vector_store,
        )
    finally:
        if embedder is not None and hasattr(embedder, "close"):
            embedder.close()
        engine.dispose()
    typer.echo(
        f"✅ 已建索引 index={result.index_version_id} embed={label}"
        f" chunks={result.chunk_count} chunking={result.chunking_version[:12]}…"
        f" 状态={result.status}"
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
    timeout: Annotated[
        float, typer.Option("--timeout", help="单次 LLM 调用超时秒数（长章/长 prompt 需更长）")
    ] = 90.0,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只验证配置与章节，不调用模型")
    ] = False,
) -> None:
    """逐章 Map 抽取 ExtractionDraftV1（阶段 06）。"""
    _log_command_invoked("extract")
    engine = _open_db()
    try:
        _run_extract(
            engine,
            book_id,
            limit=limit,
            concurrency=concurrency,
            dry_run=dry_run,
            timeout_seconds=timeout,
        )
    finally:
        engine.dispose()


def _cli_generation_profile(concurrency: int, timeout_seconds: float = 90.0) -> GenerationProfile:
    """从 AppSettings 构造 CLI generation profile（密钥只读环境，不落库）。

    LLM_* / NOVELCANON_LLM_* 环境变量与 .env（gitignore 已忽略）由
    pydantic-settings 统一加载；llm_api_key 字段 exclude=True，
    不进 config_hash / 日志 / 数据库。

    timeout_seconds：单次 LLM 调用超时（默认 90s——阶段 11 真实语料
    长章 + 逐字 prompt 更长，60s 默认对 1.2 万字章不足导致超时）。
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
        timeout_seconds=timeout_seconds,
    )
    if not settings.llm_model or not settings.llm_base_url:
        raise typer.BadParameter(
            "缺少模型配置：请设置 LLM_MODEL 与 LLM_BASE_URL"
            "（API key 用 LLM_API_KEY，本地 provider 可省略；均可写入 .env）"
        )
    return profile


def _is_real_chapter(ch: dict) -> bool:
    """正文章判定：跳过目录页与空章/版权页等噪音条目（无实质正文）。"""
    if ch["title"] == "（目录）":
        return False
    chars = (ch["char_end"] or 0) - (ch["char_start"] or 0)
    return chars >= 50


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
    timeout_seconds: float = 90.0,
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

    profile = _cli_generation_profile(concurrency, timeout_seconds=timeout_seconds)
    prompts = default_map_prompts()
    repo = Repository(engine)
    chapters = [ch for ch in repo.list_chapters(book_id) if _is_real_chapter(ch)]
    if limit is not None:
        chapters = chapters[:limit]
    if not chapters:
        typer.echo(f"❌ book={book_id} 没有可抽取章节，请先 novelcanon import")
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
        # P1（十五轮）：Map 复用只复制 draft，不关联来源 run 的已物化 claims
        # ——否则证据版本升级后旧 claim 泄漏进 active（supported 无 evidence）。
        reuse_materialized_products=False,
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


@app.command("link")
def link(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
    run_id: Annotated[
        str | None, typer.Option(help="要链接的 run_id；缺省取最新 running run")
    ] = None,
) -> None:
    """阶段 09：跨章事件链接（causes/enables，可验证因果）。"""
    _log_command_invoked("link")
    engine = _open_db()
    try:
        _run_link(engine, book_id, run_id=run_id)
    finally:
        engine.dispose()


def _run_link(engine: Engine, book_id: str, *, run_id: str | None = None) -> None:
    """读取 run 的 event claims，生成并落库跨章因果链接。"""
    from novelcanon.events import EventLinkService
    from novelcanon.pipeline.run import RunManager

    if run_id is None:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT run_id FROM extraction_runs WHERE book_id = :b"
                    " AND status = 'running' ORDER BY started_at DESC LIMIT 1"
                ),
                {"b": book_id},
            ).fetchone()
        if row is None:
            typer.echo(f"❌ book={book_id} 没有 running run，请先 novelcanon extract")
            raise typer.Exit(1)
        run_id = row[0]
    run = RunManager(engine).get(run_id)
    if run is None or run["book_id"] != book_id:
        typer.echo(f"❌ run={run_id} 不存在或不属于 book={book_id}")
        raise typer.Exit(1)

    service = EventLinkService(engine)
    stats = service.link_run(run_id, book_id)
    typer.echo(
        f"✅ 事件链接 run={run_id}：events={stats.events}"
        f" candidates={stats.candidates} links={stats.links}"
        f" unverified={stats.unverified}"
    )
    if stats.statuses:
        typer.echo(
            "   状态分布：" + " ".join(f"{k}={v}" for k, v in sorted(stats.statuses.items()))
        )
    typer.echo("   run 状态保持 running（阶段 10 查询检索与分层摘要）")


@app.command("resolve")
def resolve(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
    run_id: Annotated[
        str | None, typer.Option(help="要消歧的 run_id；缺省取最新 running run")
    ] = None,
) -> None:
    """阶段 08：实体消歧（mention → canonical，跨 run 稳定）。"""
    _log_command_invoked("resolve")
    engine = _open_db()
    try:
        _run_resolve(engine, book_id, run_id=run_id)
    finally:
        engine.dispose()


def _run_resolve(engine: Engine, book_id: str, *, run_id: str | None = None) -> None:
    """读取 run 的 mention，执行确定性消歧并落库投影/审计。"""
    from novelcanon.pipeline.run import RunManager
    from novelcanon.resolution import ResolutionService

    if run_id is None:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT run_id FROM extraction_runs WHERE book_id = :b"
                    " AND status = 'running' ORDER BY started_at DESC LIMIT 1"
                ),
                {"b": book_id},
            ).fetchone()
        if row is None:
            typer.echo(f"❌ book={book_id} 没有 running run，请先 novelcanon extract")
            raise typer.Exit(1)
        run_id = row[0]
    run = RunManager(engine).get(run_id)
    if run is None or run["book_id"] != book_id:
        typer.echo(f"❌ run={run_id} 不存在或不属于 book={book_id}")
        raise typer.Exit(1)

    service = ResolutionService(engine)
    stats = service.resolve_run(run_id, book_id)
    typer.echo(
        f"✅ 实体消歧 run={run_id}：mentions={stats.mentions}"
        f" mapped={stats.mapped} unresolved={stats.unresolved}"
        f" new_entities={stats.new_entities} merges={stats.merges}"
    )
    with engine.connect() as conn:
        canonical_count = conn.execute(
            text("SELECT COUNT(DISTINCT canonical_id) FROM entity_resolutions WHERE run_id = :r"),
            {"r": run_id},
        ).scalar()
    typer.echo(f"   canonical 实体数={canonical_count}")
    typer.echo("   run 状态保持 running（阶段 09 事件链接与双时间后 activate）")


@app.command("align")
def align(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
    run_id: Annotated[
        str | None, typer.Option(help="要对齐的 run_id；缺省取最新 running run")
    ] = None,
) -> None:
    """阶段 07：staging Map Draft → 证据对齐 → materialize（run 保持 running）。"""
    _log_command_invoked("align")
    engine = _open_db()
    try:
        _run_align(engine, book_id, run_id=run_id)
    finally:
        engine.dispose()


def _run_align(engine: Engine, book_id: str, *, run_id: str | None = None) -> None:
    """读取 staging valid Draft，执行证据对齐并 materialize；run 保持 running。"""
    from novelcanon.evidence import EvidenceService
    from novelcanon.pipeline.run import RunManager
    from novelcanon.schemas.types import RunStatus
    from novelcanon.storage.repository import Repository

    repo = Repository(engine)
    if run_id is None:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT run_id FROM extraction_runs WHERE book_id = :b"
                    " AND status = 'running' ORDER BY started_at DESC LIMIT 1"
                ),
                {"b": book_id},
            ).fetchone()
        if row is None:
            typer.echo(f"❌ book={book_id} 没有 running run，请先 novelcanon extract")
            raise typer.Exit(1)
        run_id = row[0]
    run = RunManager(engine).get(run_id)
    if run is None or run["book_id"] != book_id:
        typer.echo(f"❌ run={run_id} 不存在或不属于 book={book_id}")
        raise typer.Exit(1)
    if run["status"] != RunStatus.RUNNING.value:
        typer.echo(f"⚠️  run={run_id} 状态为 {run['status']}（期望 running），继续对齐")
    if not repo.list_valid_map_drafts(run_id):
        typer.echo(f"❌ run={run_id} 没有 valid staging Draft，请先 novelcanon extract")
        raise typer.Exit(1)

    service = EvidenceService(engine)
    stats = service.align_run(run_id, book_id)
    typer.echo(
        f"✅ 证据对齐 run={run_id}：章节={stats.chapters}"
        f" claims={stats.claims} evidence={stats.evidence}"
    )
    typer.echo("   状态分布：" + " ".join(f"{k}={v}" for k, v in sorted(stats.statuses.items())))
    if stats.errors:
        by_code: dict[str, int] = {}
        for e in stats.errors:
            by_code[e["error_code"]] = by_code.get(e["error_code"], 0) + 1
        typer.echo("   ⚠️  证据错误：" + " ".join(f"{k}={v}" for k, v in sorted(by_code.items())))
    typer.echo("   run 状态保持 running（阶段 09 事件链接与双时间后 activate）")


@app.command()
def activate(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
    run_id: Annotated[
        str | None, typer.Option(help="要激活的 run_id；缺省取最新可激活 run")
    ] = None,
) -> None:
    """原子激活已完成验证的 run（阶段 04 实现；激活后结构化查询可见）。"""
    _log_command_invoked("activate")
    engine = _open_db()
    try:
        _run_activate(engine, book_id, run_id=run_id)
    finally:
        engine.dispose()


def _run_activate(engine: Engine, book_id: str, *, run_id: str | None = None) -> None:
    """状态机推进到 ready_to_activate，再原子激活（旧 active → superseded）。"""
    from novelcanon.pipeline import RunManager
    from novelcanon.pipeline.validation import Activator
    from novelcanon.schemas.types import RunStatus

    mgr = RunManager(engine)
    if run_id is None:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT run_id FROM extraction_runs WHERE book_id = :b"
                    " AND status IN ('running','validating','ready_to_activate')"
                    " ORDER BY started_at DESC LIMIT 1"
                ),
                {"b": book_id},
            ).fetchone()
        if row is None:
            typer.echo(f"❌ book={book_id} 没有可激活 run，请先 novelcanon extract")
            raise typer.Exit(1)
        run_id = row[0]
    run = mgr.get(run_id)
    if run is None or run["book_id"] != book_id:
        typer.echo(f"❌ run={run_id} 不存在或不属于 book={book_id}")
        raise typer.Exit(1)
    if run["status"] in {
        s.value for s in (RunStatus.ACTIVE, RunStatus.SUPERSEDED, RunStatus.FAILED)
    }:
        typer.echo(f"❌ run={run_id} 已是终态 {run['status']}")
        raise typer.Exit(1)

    # 状态机推进：created → running → validating → ready_to_activate
    order = (
        RunStatus.CREATED,
        RunStatus.RUNNING,
        RunStatus.VALIDATING,
        RunStatus.READY_TO_ACTIVATE,
    )
    current = RunStatus(run["status"])
    for target in order:
        if current == target:
            continue
        if not mgr.transition(run_id, current, target):
            typer.echo(f"❌ 状态推进失败：{current.value} → {target.value}")
            raise typer.Exit(1)
        current = target

    issues = Activator(engine).activate(run_id)
    if issues:
        typer.echo("❌ 激活失败：" + "; ".join(issues))
        raise typer.Exit(1)
    with engine.connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM v_active_claims WHERE book_id = :b"),
            {"b": book_id},
        ).scalar()
    typer.echo(f"✅ run={run_id} 已激活（active）；active claims={n}，结构化查询现可访问")


@app.command()
def query(
    question: Annotated[str, typer.Argument(help="自然语言问题")],
    book_id: Annotated[str, typer.Option(help="book_id（novelcanon inspect 可查）")],
    cutoff: Annotated[
        int | None,
        typer.Option(help="knowledge cutoff 章节（读者披露截止）"),
    ] = None,
    world: Annotated[int | None, typer.Option(help="world at 章节（世界时间点）")] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="跳过缓存")] = False,
) -> None:
    """结构化查询 / 混合检索 / 证据接地问答（阶段 10）。"""
    _log_command_invoked("query")
    engine = _open_db()
    try:
        _run_query(engine, question, book_id, cutoff=cutoff, world=world, no_cache=no_cache)
    finally:
        engine.dispose()


def _run_query(
    engine: Engine,
    question: str,
    book_id: str,
    *,
    cutoff: int | None,
    world: int | None,
    no_cache: bool,
) -> None:
    """路由 → 执行 → 合成 → 输出（含 explain 与路线统计）。"""
    from novelcanon.query import QueryExecutor, route_question
    from novelcanon.retrieval.factory import NoActiveIndexError, backend_for_active_index

    decision = route_question(question)
    typer.echo(
        f"🧭 路由：type={decision.query_type} route={decision.route}"
        f"{'（fallback）' if decision.is_fallback else ''}"
    )
    typer.echo(f"   {decision.explain}")

    # 按 active index 的 embedding profile 创建运行时后端（阶段 11 复审 D
    # 统一入口）：真实索引（text-embedding-v4）必须用真实 adapter，否则
    # profile mismatch；**仅无 active 索引**（NoActiveIndexError）时结构化
    # 查询 fake 兜底——配置校验错误（缺 dimension/base_url）是 ValueError，
    # 必须原样上报，不得静默回退 fake 再产生误导性 mismatch（复审 D P2）。
    try:
        embedder, vector_store = backend_for_active_index(engine, book_id)
    except NoActiveIndexError:
        from novelcanon.retrieval.vectorstore import BruteForceVectorStore, FakeEmbedder

        embedder, vector_store = FakeEmbedder(dimension=8), BruteForceVectorStore(dimension=8)
    try:
        executor = QueryExecutor(
            engine,
            book_id,
            embedder=embedder,
            vector_store=vector_store,
            use_cache=not no_cache,
        )
        result = executor.ask(question, knowledge_cutoff=cutoff, world_at=world)
    finally:
        closer = getattr(embedder, "close", None)
        if closer is not None:
            closer()
    payload = result.answer
    if result.cached:
        typer.echo("♻️  命中缓存（active run/index 签名一致）")
    typer.echo("─" * 60)
    typer.echo(f"📖 回答（route={payload['route']}）：\n{payload['answer']}")
    typer.echo("─" * 60)
    typer.echo(f"置信度={payload['confidence']} 上下文={payload['context_id'][:12]}…")
    typer.echo(f"cutoff={payload['knowledge_cutoff']} world_at={payload['world_at']}")
    if payload["caveats"]:
        typer.echo("⚠️  " + "；".join(payload["caveats"]))
    if payload["sources"]:
        typer.echo(f"证据来源（{len(payload['sources'])}）：")
        for s in payload["sources"][:10]:
            typer.echo(
                f"   - [{s['kind']}] 章{s['observed_ordinal']}"
                f" 立场={s['stance']} {s['claim_version_id'][:16]}…"
            )
    else:
        typer.echo("（无证据来源：证据不足，明确拒答）")
    typer.echo("─" * 60)
    typer.echo("按路线统计：")
    for route, st in executor.stats().items():
        typer.echo(
            f"   {route}: calls={st['calls']} 延迟={st['latency_ms']}ms"
            f" 上下文项={st['context_items']} 命中={st['hits']}"
            f" 缓存命中={st['cache_hits']}"
            f" tokens={st['input_tokens']}in/{st['output_tokens']}out"
        )


@app.command("group-volumes")
def group_volumes(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
    chunk: Annotated[int, typer.Option(help="默认每 N 章一组")] = 50,
) -> None:
    """卷分组（阶段 10 §6）：原书卷标题优先，缺失时每 N 章默认分组。"""
    _log_command_invoked("group-volumes")
    engine = _open_db()
    try:
        from novelcanon.summaries import VolumeGrouper

        result = VolumeGrouper(engine, book_id, chapters_per_volume=chunk).group()
        typer.echo(
            f"✅ 卷分组 book={book_id}：版本={result.grouping_version}"
            f" 来源={result.source} 卷数={len(result.volumes)}"
            f" 重建={result.rebuilt} 变化={result.changed}"
        )
        for v in result.volumes:
            typer.echo(
                f"   {v.ordinal}. {v.title}：第{v.start_ordinal}–{v.end_ordinal}章"
                f"（{v.grouping_source}）"
            )
    finally:
        engine.dispose()


@app.command()
def summarize(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
    cutoff: Annotated[int | None, typer.Option(help="knowledge cutoff（不泄露其后内容）")] = None,
    deterministic: Annotated[
        bool,
        typer.Option(
            "--deterministic",
            help="用确定性提取式摘要（无模型）；缺省用 LLM（需 LLM_* 配置）",
        ),
    ] = False,
) -> None:
    """分层 Reduce：章节 → 卷 → 全书摘要（阶段 10 §7）。"""
    _log_command_invoked("summarize")
    engine = _open_db()
    try:
        _run_summarize(engine, book_id, cutoff=cutoff, deterministic=deterministic)
    finally:
        engine.dispose()


def _run_summarize(
    engine: Engine, book_id: str, *, cutoff: int | None, deterministic: bool
) -> None:
    from novelcanon.summaries import (
        DeterministicSummarizer,
        HierarchicalReducer,
        LLMSummarizer,
    )

    if deterministic:
        summarizer: object = DeterministicSummarizer()
        label = "确定性提取式"
    else:
        profile = _cli_generation_profile(1)
        tokenizer = _tokenizer_for(profile)
        api_key = AppSettings().llm_api_key or None
        if api_key is None and profile.api_key_env:
            typer.echo(f"⚠️  未设置 {profile.api_key_env}；若 provider 需要鉴权将失败")
        from novelcanon.generation.client import GenerationClient

        client = GenerationClient(profile, tokenizer=tokenizer, api_key=api_key)
        summarizer = LLMSummarizer(
            client,
            profile_id=profile.profile_id,
            prompt_version=f"llm-summary-{profile.config_hash[:10]}",
        )
        label = f"LLM（{profile.model}）"

    reducer = HierarchicalReducer(engine, book_id, summarizer=summarizer)  # type: ignore[arg-type]
    result = reducer.reduce(cutoff=cutoff)
    typer.echo(
        f"✅ 分层摘要 book={book_id}（{label}）："
        f"分组版本={result.grouping.grouping_version}"
        f" 章节记忆={result.chapter_memories}"
        f" 卷摘要={len(result.volume_summaries)}"
        f" 全书摘要={'有' if result.book_summary else '无'}"
        f" 新建={result.rebuilt} 复用={result.reused} 失效={result.stale}"
    )
    for s in result.volume_summaries:
        typer.echo(
            f"   卷「{s['title']}」 max_ordinal={s['max_observed_ordinal']}"
            f" 输入claim={len(json.loads(s['input_claim_versions']))}"
            f" 内容hash={s['content_hash'][:12]}…"
        )
    if result.book_summary:
        s = result.book_summary
        typer.echo(
            f"   全书「{s['title']}」 max_ordinal={s['max_observed_ordinal']}"
            f" 依赖卷摘要={len(json.loads(s['depends_on_summaries']))}"
        )
    if result.tokens.total() > 0:
        typer.echo(
            f"   token 计量：{result.tokens.input_tokens}in/"
            f"{result.tokens.output_tokens}out"
            f" 重试={result.tokens.retry_count}"
        )


@app.command()
def stats(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
) -> None:
    """查询与摘要统计（10 退出标准：质量/延迟/Token 按路线统计）。"""
    _log_command_invoked("stats")
    engine = _open_db()
    try:
        _run_stats(engine, book_id)
    finally:
        engine.dispose()


def _run_stats(engine: Engine, book_id: str) -> None:
    with engine.connect() as conn:
        cache_rows = conn.execute(
            text(
                "SELECT query_type, COUNT(*) AS n,"
                " SUM(length(result)) AS bytes"
                " FROM query_cache WHERE book_id = :b GROUP BY query_type"
            ),
            {"b": book_id},
        ).fetchall()
        summary_rows = conn.execute(
            text(
                "SELECT level, status, COUNT(*) AS n"
                " FROM summary_artifacts WHERE book_id = :b"
                " GROUP BY level, status"
            ),
            {"b": book_id},
        ).fetchall()
        volumes = conn.execute(
            text("SELECT COUNT(*) FROM volumes WHERE book_id = :b"),
            {"b": book_id},
        ).scalar()
    typer.echo(f"📊 book={book_id} 统计：")
    typer.echo(f"   卷行（全版本）={volumes}")
    typer.echo("   查询缓存（按类型）：")
    for qtype, n, _bytes in cache_rows:
        typer.echo(f"     {qtype}: {n} 条")
    typer.echo("   摘要产物（按级别/状态）：")
    for level, status, n in summary_rows:
        typer.echo(f"     {level}/{status}: {n}")
    typer.echo("   提示：query 命令输出含按路线延迟/命中统计（QueryExecutor.stats）")


@app.command()
def pilot(
    book_id: Annotated[str, typer.Argument(help="book_id（黄金样例库，novelcanon inspect 可查）")],
    cutoff: Annotated[int | None, typer.Option(help="knowledge cutoff（评测截断）")] = None,
    out: Annotated[Path | None, typer.Option("--out", help="报告 JSON 输出路径")] = None,
    compressed: Annotated[
        bool, typer.Option("--compressed", help="运行压缩路线（决策门）")
    ] = False,
    golden: Annotated[
        Path | None,
        typer.Option(
            "--golden",
            help="冻结黄金集 JSON 文件（正式 P1/P2 评测必须提供；缺省用 4 章 fixture 黄金集）",
        ),
    ] = None,
    extraction: Annotated[
        str | None,
        typer.Option(
            "--extraction-mode",
            help="压缩重抽取模式：llm-map（正式，真实 LLM Map 抽取，需 LLM 配置）"
            " / golden-replay（oracle 辅助验证，只适合 fixture，不能授权启用压缩）；"
            "缺省：--golden 正式模式自动 llm-map（未配置 LLM 报错），fixture 自动 golden-replay",
        ),
    ] = None,
) -> None:
    """短篇 Pilot：黄金集评测（11 §P1，结构化 + hybrid + 可选压缩路线）。"""
    _log_command_invoked("pilot")
    engine = _open_db()
    try:
        _run_pilot(
            engine,
            book_id,
            cutoff=cutoff,
            out=out,
            compressed=compressed,
            golden_path=golden,
            extraction_mode=extraction,
        )
    finally:
        engine.dispose()


def _run_pilot(
    engine: Engine,
    book_id: str,
    *,
    cutoff: int | None,
    out: Path | None,
    compressed: bool = False,
    golden_path: Path | None = None,
    extraction_mode: str | None = None,
) -> None:
    import json

    from novelcanon.eval import (
        golden_set_from_chapters,
        golden_set_from_file,
        run_pilot,
        validate_golden_against_book,
    )

    if extraction_mode not in (None, "llm-map", "golden-replay"):
        typer.echo(
            f"❌ --extraction-mode 仅支持 llm-map / golden-replay：{extraction_mode!r}",
            err=True,
        )
        raise typer.Exit(code=1)

    if golden_path is not None:
        golden = golden_set_from_file(golden_path)
        # 正式评测强制书内容 hash（P1：防止同 book_id 旧标注错配到已修改文本）
        errors = validate_golden_against_book(engine, book_id, golden, require_content_hash=True)
        if errors:
            for e in errors:
                typer.echo(f"❌ 黄金集校验失败：{e}", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"✅ 黄金集已加载并校验通过：{golden_path}")
    else:
        golden = golden_set_from_chapters(book_id)
        typer.echo(
            "⚠️  使用 4 章 fixture 黄金集（仅用于黄金样例验收；正式 P1/P2 评测请用 --golden）"
        )

    # 压缩重抽取器（复审 P0：正式路径必须注入生产 Map 抽取器，不得静默
    # 回退 golden-replay）：
    # - --extraction-mode llm-map 或正式（--golden）+ --compressed：构造
    #   生产 LLM Map 抽取器（未配置 LLM → 报错退出）；
    # - --extraction-mode golden-replay：显式 oracle 辅助验证（fixture）；
    # - fixture（无 --golden）+ --compressed：允许 golden-replay（开发）。
    claim_extractor = None
    want_llm = extraction_mode == "llm-map" or (extraction_mode is None and golden_path is not None)
    if compressed and want_llm:
        from novelcanon.eval.extractor import build_map_extractor

        try:
            claim_extractor = build_map_extractor()
        except RuntimeError as exc:
            typer.echo(f"❌ {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo("✅ 压缩重抽取：生产 LLM Map 抽取器（llm-map，真实抽取 recall）")
    elif compressed:
        typer.echo(
            "⚠️  压缩重抽取：golden-replay（oracle 辅助验证，只适合 fixture；"
            "不能授权启用压缩——决策门 extraction_mode 硬前置拒绝）"
        )

    report = run_pilot(
        engine,
        book_id,
        golden,
        cutoff=cutoff,
        compressed=compressed,
        claim_extractor=claim_extractor,
    )
    payload = report.to_dict()
    if out is not None:
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        typer.echo(f"✅ Pilot 报告已写入 {out}")
    typer.echo(f"📊 Pilot book={book_id}（cutoff={cutoff}）：")
    for route, metrics in sorted(payload["routes"].items()):
        if metrics.get("status"):
            typer.echo(f"   {route}: {metrics['status']}")
            continue
        qa = metrics.get("qa_chapter_accuracy", {})
        facts = metrics.get("facts", {})
        if route == "compressed":
            decision = metrics.get("decision", {})
            usage = metrics.get("usage", {})
            per_wan = usage.get("per_wan", {})
            typer.echo(
                f"   {route}: QA={qa.get('accuracy', 0.0)} 事实 recall="
                f"{facts.get('recall', 0.0)} 保留率={metrics.get('retention', 0.0)}"
                f" 抽取模式={metrics.get('extraction_mode', '')}"
                f" 每万字token={per_wan.get('tokens', 0.0)}"
                f" 决策门={'启用' if decision.get('enable') else '不启用'}"
            )
            continue
        merges = metrics.get("entity_merges", {})
        merge_all = merges.get("all", {})
        merge_core = merges.get("core", {})
        usage = metrics.get("usage", {})
        per_wan = usage.get("per_wan", {})
        typer.echo(
            f"   {route}: QA 章节正确率={qa.get('accuracy', 0.0)}"
            f" 事实 F1={facts.get('f1', 0.0)}"
            f" 实体合并 F1 all/core={merge_all.get('f1', 0.0)}/"
            f"{merge_core.get('f1', 0.0)}"
            f" 延迟={metrics.get('latency_ms')}ms"
            f" 吞吐={usage.get('throughput_qps', 0.0)}qps"
            f" 每万字token={per_wan.get('tokens', 0.0)}"
        )


@app.command()
def compress(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
    chapters: Annotated[int | None, typer.Option(help="只压缩前 N 章")] = None,
) -> None:
    """压缩实验（11 §压缩实验）：规则预扫描 → 改写 → 后验校验（确定性）。"""
    _log_command_invoked("compress")
    engine = _open_db()
    try:
        _run_compress(engine, book_id, chapters=chapters)
    finally:
        engine.dispose()


def _run_compress(engine: Engine, book_id: str, *, chapters: int | None) -> None:
    from novelcanon.compression import CompressionService
    from novelcanon.storage.repository import Repository

    repo = Repository(engine)
    chs = repo.list_chapters(book_id)
    if chapters is not None:
        chs = chs[:chapters]
    if not chs:
        typer.echo(f"❌ book={book_id} 没有章节")
        raise typer.Exit(1)
    full = repo.get_book_text(book_id)
    # 已知实体表面名（供保留词典）
    with engine.connect() as conn:
        surfaces = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT surface_name FROM entity_alias_claims"
                    " WHERE canonical_id IN (SELECT canonical_id FROM entities)"
                    " LIMIT 200"
                )
            ).fetchall()
        ]
    texts = [(c["chapter_id"], full[c["char_start"] : c["char_end"]]) for c in chs]
    service = CompressionService()
    results = service.compress_book(texts, known_surfaces=surfaces)
    total_in = sum(len(r.original_text) for r in results)
    total_out = sum(len(r.compressed_text) for r in results)
    dropped = sum(r.result.dropped for r in results)
    fallback = sum(r.validation.get("fallback_segments", 0) for r in results)
    version = results[0].compression_version if results else ""
    typer.echo(
        f"✅ 压缩实验 book={book_id} 章数={len(results)}："
        f"保留率={total_out / total_in:.2%}"
        f" drop 段={dropped} 回退段={fallback}"
    )
    typer.echo(f"   compression version={version[:20]}…")
    passed = sum(1 for r in results if r.passed_validation)
    typer.echo(f"   后验通过章={passed}/{len(results)}")


@app.command()
def backup(
    out: Annotated[Path, typer.Argument(help="备份文件路径")],
) -> None:
    """备份数据库（在线备份，WAL 安全；11 §P3）。"""
    _log_command_invoked("backup")
    from novelcanon.storage.backup import backup_database

    engine = _open_db()
    try:
        result = backup_database(engine, out)
    finally:
        engine.dispose()
    meta = result.meta
    typer.echo(
        f"✅ 备份完成 {out}：books={meta['books']} chapters={meta['chapters']}"
        f" claims={meta['claims']} evidence={meta['evidence']}"
    )


@app.command()
def restore(
    backup_path: Annotated[Path, typer.Argument(help="备份文件路径")],
    db_path: Annotated[Path, typer.Argument(help="恢复目标数据库路径")],
) -> None:
    """恢复数据库并校验完整性（FK/证据 hash；11 §P3/P5）。"""
    _log_command_invoked("restore")
    from novelcanon.storage.backup import restore_database

    engine = _open_db()
    try:
        report = restore_database(engine, backup_path, db_path)
    finally:
        engine.dispose()
    typer.echo(
        f"✅ 恢复完成 {db_path}：FK 违规={report.fk_violations}"
        f" 证据复现={report.evidence_reproduced}/{report.evidence_checked}"
        f" claims 重复={report.claim_duplicates} 完整性={'通过' if report.ok else '失败'}"
    )


@app.command()
def scan_leakage(
    book_id: Annotated[str, typer.Argument(help="book_id（novelcanon inspect 可查）")],
) -> None:
    """cutoff 泄露扫描（11 §P5：泄露回归为零）。"""
    _log_command_invoked("scan-leakage")
    engine = _open_db()
    try:
        _run_scan_leakage(engine, book_id)
    finally:
        engine.dispose()


def _run_scan_leakage(engine: Engine, book_id: str) -> None:
    from novelcanon.ops import scan_cutoff_leakage

    questions = ["萧炎的修为状态", "第二章发生了什么", "这本书的主线是什么"]
    cutoffs = [1, 2, 5]
    result = scan_cutoff_leakage(engine, book_id, questions, cutoffs)
    typer.echo(
        f"🔍 cutoff 泄露扫描 book={book_id}：检查={result.checks}"
        f" 泄露={'有' if result.leaked else '无'}"
    )
    for f in result.findings:
        typer.echo(f"   ⚠️  {f.question} cutoff={f.cutoff} 泄露章节={f.leaked_ordinals}")
    if result.leaked:
        raise typer.Exit(1)


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="监听端口")] = 8000,
    host: Annotated[str, typer.Option(help="监听地址")] = "127.0.0.1",
) -> None:
    """启动查询 API（11 §P4；复用 CLI 的 application service）。"""
    _log_command_invoked("serve")
    import uvicorn

    typer.echo(f"🚀 查询 API 启动 http://{host}:{port}（阶段 11 P4）")
    uvicorn.run("novelcanon.api:app", host=host, port=port)


@app.command()
def run_abandon(
    run_id: Annotated[str, typer.Argument(help="要放弃的 run_id（novelcanon inspect 可查）")],
    reason: Annotated[str, typer.Option("--reason", help="放弃原因（写入 run.error 审计）")] = (
        "superseded by newer run"
    ),
) -> None:
    """人工放弃未激活的 run（created/running/validating → abandoned）。

    与 failed（执行失败）语义区分：开发抽查、被新 run 取代的陈旧 run
    应标记 abandoned，避免污染运维统计（阶段 11：真实语料运行遗留
    2 个 running run 后补充）。active run 禁止放弃。
    """
    _log_command_invoked("run-abandon")
    from novelcanon.pipeline import RunManager
    from novelcanon.schemas.types import RunStatus

    engine = _open_db()
    try:
        mgr = RunManager(engine)
        run = mgr.get(run_id)
        if run is None:
            typer.echo(f"❌ run 不存在：{run_id}", err=True)
            raise typer.Exit(code=1)
        if run["status"] == RunStatus.ACTIVE.value:
            typer.echo(f"❌ active run 禁止放弃：{run_id}", err=True)
            raise typer.Exit(code=1)
        if run["status"] == RunStatus.ABANDONED.value:
            typer.echo(f"ℹ️  已是 abandoned：{run_id}")
            return
        if mgr.abandon(run_id, reason):
            typer.echo(f"✅ run={run_id} 已标记 abandoned（原状态 {run['status']}）：{reason}")
        else:
            typer.echo(f"❌ 放弃失败（仅 created/running/validating 可放弃）：{run_id}", err=True)
            raise typer.Exit(code=1)
    finally:
        engine.dispose()


@app.command()
def inspect() -> None:
    """检查库内对象（book/run/chapter 等；阶段 02 起逐步实现）。"""
    _log_command_invoked("inspect")
    typer.echo("尚未实现：inspect（阶段 02 数据契约与存储）")


if __name__ == "__main__":
    app()
