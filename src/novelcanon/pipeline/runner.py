"""worker / 有界队列 / 单 writer（阶段 04，docs/implementation/04 §3）。

- worker 只产出通过校验的 ProcessResult（或结构化错误）；
- 结果经有界队列（背压）交给单 writer；
- writer 批量事务写 checkpoint + ledger（一批全成或全回滚）；
- checkpoint 命中复用：不调用 provider，但记录来源 run。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from novelcanon.pipeline.checkpoint import CheckpointService
from novelcanon.pipeline.ledger import LedgerEntry, TokenLedger, Usage
from novelcanon.pipeline.ratelimit import (
    NonRetryableError,
    RetryableError,
    RetryPolicy,
    TokenBucket,
)
from novelcanon.schemas.types import RunStatus
from novelcanon.storage.repository import Repository


@dataclass(frozen=True)
class ChapterTask:
    """单章任务：checkpoint_fields 为唯一键字段（含 content_hash 与各版本）。"""

    chapter_id: str
    ordinal: int
    content: str
    checkpoint_fields: dict[str, object]


@dataclass(frozen=True)
class ProcessResult:
    """worker 产出：payload 已通过 Schema 校验（或 failed 携带结构化错误）。"""

    payload: dict = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    failed: bool = False
    error: str | None = None


@dataclass(frozen=True)
class RunSummary:
    total: int
    completed: int
    reused: int
    failed: int
    failed_chapters: list[str]
    writer_failures: int


ProcessFn = Callable[[ChapterTask], Awaitable[ProcessResult]]
SchemaCheck = Callable[[dict], bool]

Item = tuple[str, ChapterTask, dict | ProcessResult]


class StagingWriter(Protocol):
    """阶段 06：Map 产物 staging 写入（map_drafts；writer 事务内调用）。"""

    def write(
        self,
        conn: Connection,
        run_id: str,
        task: ChapterTask,
        payload: dict,
        *,
        source_run_id: str | None = None,
        error: str | None = None,
    ) -> None: ...


class PipelineRunner:
    """并发 worker + 有界队列 + 单 writer 批处理。"""

    def __init__(
        self,
        engine: Engine,
        run_id: str,
        book_id: str,
        *,
        concurrency: int = 2,
        queue_size: int = 100,
        batch_size: int = 10,
        retry_policy: RetryPolicy | None = None,
        limiter: TokenBucket | None = None,
        checkpoint: CheckpointService | None = None,
        ledger: TokenLedger | None = None,
        staging: StagingWriter | None = None,
        reuse_materialized_products: bool = True,
    ) -> None:
        """reuse_materialized_products：checkpoint 复用是否把来源 run 的**已
        物化下游产物**（claims/aliases/mentions 成员关系）关联到新 run。

        阶段 11 十五轮 P1：Map 阶段（staging=MapStaging）必须关闭——Map
        复用只应复制 draft；若连带关联来源 run 的 claims，证据版本升级
        （v1→v2）后未通过新 align 的旧 claim 会继续进入 active（真实数据
        出现 1 条 supported 无 evidence 的 state claim）。align/link 等
        下游阶段可开启（其复用依赖成员关系）。
        """
        self._engine = engine
        self._run_id = run_id
        self._book_id = book_id
        self._concurrency = max(1, concurrency)
        self._queue_size = max(1, queue_size)
        self._batch_size = max(1, batch_size)
        self._policy = retry_policy or RetryPolicy()
        self._limiter = limiter
        self._checkpoint = checkpoint or CheckpointService(engine)
        self._ledger = ledger or TokenLedger(engine)
        self._repo = Repository(engine)
        self._staging = staging
        self._reuse_materialized_products = reuse_materialized_products

    async def run(
        self,
        tasks: list[ChapterTask],
        process_fn: ProcessFn,
        *,
        stage: str = "map",
        timeout_seconds: float = 10.0,
        schema_check: SchemaCheck | None = None,
    ) -> RunSummary:
        """执行一批章节任务；返回汇总（completed/reused/failed）。

        - checkpoint 命中 → 复用（不调用 provider，source_run_id=旧 run）；
        - 未命中 → process_fn（超时 + 指数退避重试，不可重试错误直判失败）；
        - writer 批量事务落 checkpoint + ledger；失败整批回滚（writer_failures+1）。
        """
        queue: asyncio.Queue[Item] = asyncio.Queue(maxsize=self._queue_size)
        total = len(tasks)
        stats = {"new": 0, "reuse": 0, "failed": 0, "writer_failures": 0}
        failed_chapters: list[str] = []
        lock = asyncio.Lock()

        async def worker(task: ChapterTask) -> None:
            hit = self._checkpoint.find_done(task.checkpoint_fields)
            if hit is not None:
                await queue.put(("reuse", task, hit))
                return
            result = await self._process_with_retry(
                task, process_fn, timeout_seconds, schema_check, stage
            )
            await queue.put(("new", task, result))

        def tally(batch: list[Item]) -> None:
            for kind, task, result in batch:
                if kind == "reuse":
                    stats["reuse"] += 1
                elif isinstance(result, ProcessResult) and result.failed:
                    stats["failed"] += 1
                    failed_chapters.append(task.chapter_id)
                else:
                    stats["new"] += 1

        async def writer() -> None:
            processed = 0
            batch: list[Item] = []
            while processed < total:
                item = await queue.get()
                batch.append(item)
                processed += 1
                if len(batch) >= self._batch_size or processed == total:
                    ok = self._flush_batch(batch, stage)
                    async with lock:
                        if not ok:
                            stats["writer_failures"] += 1
                        tally(batch)
                    batch = []
                queue.task_done()

        workers = [asyncio.create_task(worker(t)) for t in tasks]
        writer_task = asyncio.create_task(writer())
        await asyncio.gather(*workers)
        await queue.join()
        await writer_task

        return RunSummary(
            total=total,
            completed=stats["new"] + stats["reuse"],
            reused=stats["reuse"],
            failed=stats["failed"],
            failed_chapters=failed_chapters,
            writer_failures=stats["writer_failures"],
        )

    async def _process_with_retry(
        self,
        task: ChapterTask,
        process_fn: ProcessFn,
        timeout_seconds: float,
        schema_check: SchemaCheck | None,
        stage: str,
    ) -> ProcessResult:
        last_error = "unknown"
        retry_usage = Usage()
        for attempt in range(self._policy.max_attempts):
            caught: BaseException | None = None
            try:
                if self._limiter is not None:
                    await self._limiter.acquire()
                result = await asyncio.wait_for(process_fn(task), timeout=timeout_seconds)
                if result.failed:
                    # process_fn 返回 failed = 结构化错误已定论（内部已处理
                    # 传输重试/结构修复），直接失败，不在此处重试；payload 保留
                    # 供 staging 记录 invalid 详情。
                    # P0：失败但已发生模型调用（Map 解析/Schema 修复耗尽）——
                    # result.usage + retry_usage 必须入账，不得漏记。
                    self._ledger.record(
                        LedgerEntry(
                            run_id=self._run_id,
                            book_id=self._book_id,
                            chapter_id=task.chapter_id,
                            stage=stage,
                            usage=result.usage + retry_usage,
                        )
                    )
                    return result
                if schema_check is not None and not schema_check(result.payload):
                    # 外部 schema_check 失败：原始 result.usage 不得丢弃
                    self._ledger.record(
                        LedgerEntry(
                            run_id=self._run_id,
                            book_id=self._book_id,
                            chapter_id=task.chapter_id,
                            stage=stage,
                            usage=result.usage + retry_usage,
                        )
                    )
                    return ProcessResult(failed=True, error="schema 校验失败")
                # P1 修复：不再用 replace 覆盖 result.usage 的 provider 内部
                # retry_count；此前外层失败尝试的累计 retry_usage（runner
                # 重试 + provider 失败尝试的 token/次数）也必须写入账本——
                # 「失败后成功」与「429 后成功」的调用与 token 不丢失。
                # retry_usage 已含每次外层失败尝试的 retry_count=1
                # （attempt == 外层失败次数），直接相加，不重复计外层。
                usage = result.usage + retry_usage
                self._ledger.record(
                    LedgerEntry(
                        run_id=self._run_id,
                        book_id=self._book_id,
                        chapter_id=task.chapter_id,
                        stage=stage,
                        usage=usage,
                    )
                )
                return result
            except NonRetryableError as exc:
                return ProcessResult(failed=True, error=str(exc))
            except TimeoutError:
                last_error = f"timeout after {timeout_seconds}s"
            except RetryableError as exc:
                last_error = str(exc)
                caught = exc
            except Exception as exc:  # noqa: BLE001 —— 其余异常按可重试处理
                last_error = f"{type(exc).__name__}: {exc}"
                caught = exc
            retry_usage = retry_usage + Usage(retry_count=1)
            # P1：provider 内部重试耗尽时，失败尝试数/消耗的 prompt token
            # 由 client 附加到异常（provider_retry_count / provider_input_tokens），
            # 一并计入账本（「所有模型调用均可审计」含失败调用）。
            if caught is not None:
                provider_retries = getattr(caught, "provider_retry_count", 0)
                provider_tokens = getattr(caught, "provider_input_tokens", 0)
                if provider_retries:
                    retry_usage = retry_usage + Usage(
                        retry_count=int(provider_retries),
                        input_tokens=int(provider_tokens or 0),
                    )
            if attempt + 1 >= self._policy.max_attempts:
                break
            await asyncio.sleep(self._policy.delay_for(attempt))
        self._ledger.record(
            LedgerEntry(
                run_id=self._run_id,
                book_id=self._book_id,
                chapter_id=task.chapter_id,
                stage=stage,
                usage=retry_usage,
            )
        )
        return ProcessResult(failed=True, error=last_error)

    def _flush_batch(self, batch: list[Item], stage: str) -> bool:
        """单事务写 checkpoint + ledger；异常时整批回滚（不留下部分数据）。"""
        try:
            with self._engine.begin() as conn:
                for kind, task, result in batch:
                    if kind == "reuse":
                        assert isinstance(result, dict)
                        # checkpoint.payload 是 TEXT（JSON 字符串），需还原为 dict
                        payload = result["payload"]
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        # 原始 checkpoint 的 source_run_id 为空，真正来源是写入该
                        # checkpoint 行的 run（result["run_id"]）；链式复用则沿用
                        # 已记录的 source_run_id（来源链等价，成员关系相同）。
                        source_run_id = result.get("source_run_id") or result["run_id"]
                        self._checkpoint.save_with(
                            conn,
                            self._run_id,
                            task.checkpoint_fields,
                            payload,
                            source_run_id=source_run_id,
                        )
                        # 复用章节：仅把来源 run 明确拥有的产物关联到当前 run
                        # （成员关系按 source_run 复制，失败 run 的 staging 不泄漏）
                        # P1（十五轮）：Map 阶段关闭——复用只复制 draft，不关联
                        # 已物化 claims（否则证据版本升级后旧 claim 泄漏进 active）。
                        if self._reuse_materialized_products:
                            self._repo.associate_chapter_products(
                                conn,
                                self._run_id,
                                source_run_id,
                                str(task.checkpoint_fields["chapter_id"]),
                            )
                        if self._staging is not None:
                            self._staging.write(
                                conn,
                                self._run_id,
                                task,
                                payload,
                                source_run_id=source_run_id,
                            )
                    elif isinstance(result, ProcessResult) and result.failed:
                        self._checkpoint.save_with(
                            conn,
                            self._run_id,
                            task.checkpoint_fields,
                            {"error": result.error},
                            status="failed",
                        )
                        if self._staging is not None:
                            self._staging.write(
                                conn,
                                self._run_id,
                                task,
                                result.payload,
                                error=result.error,
                            )
                    elif isinstance(result, ProcessResult):
                        self._checkpoint.save_with(
                            conn, self._run_id, task.checkpoint_fields, result.payload
                        )
                        if self._staging is not None:
                            self._staging.write(conn, self._run_id, task, result.payload)
                        self._ledger.record_with(
                            conn,
                            LedgerEntry(
                                run_id=self._run_id,
                                book_id=self._book_id,
                                chapter_id=task.chapter_id,
                                stage=stage,
                                usage=Usage(),
                            ),
                        )
            return True
        except Exception:  # noqa: BLE001 —— writer 事务失败：整批回滚
            return False


def finish_run(
    engine: Engine,
    run_id: str,
    *,
    total_chapters: int,
    summary: RunSummary,
    error_ratio_threshold: float = 0.0,
) -> list[str] | None:
    """运行收尾：running → validating →（验证通过）→ ready → active。

    任一验证失败 → failed，旧 active run 保持可查（04 验证项）。
    返回 issues（None 表示激活成功）。
    """
    from novelcanon.pipeline.run import RunManager
    from novelcanon.pipeline.validation import Activator, Validator

    mgr = RunManager(engine)
    if not mgr.transition(run_id, RunStatus.RUNNING, RunStatus.VALIDATING):
        return ["run 不在 running 状态，无法进入验证"]

    validator = Validator(engine, error_ratio_threshold=error_ratio_threshold)
    issues = validator.issues(run_id, total_chapters=total_chapters)
    if issues:
        mgr.fail(run_id, "; ".join(issues))
        return issues

    if not mgr.transition(run_id, RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE):
        return ["run 不在 validating 状态"]
    activator = Activator(engine)
    return activator.activate(run_id)
