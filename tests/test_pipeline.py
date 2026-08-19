"""阶段 04：可恢复流水线黄金测试（docs/implementation/04 验证项）。"""

import asyncio
import hashlib

import pytest
from sqlalchemy import Engine, text

from novelcanon.pipeline import (
    ChapterTask,
    CheckpointService,
    NonRetryableError,
    PipelineRunner,
    ProcessResult,
    RetryableError,
    RetryPolicy,
    RunManager,
    TokenLedger,
    Usage,
    checkpoint_key,
    finish_run,
)
from novelcanon.pipeline.checkpoint import CHECKPOINT_FIELDS
from novelcanon.schemas.types import RunStatus
from novelcanon.storage.repository import Repository

BOOK = "book_pipe"


def _ensure_book(engine: Engine) -> None:
    """创建 book 与其章节（run_checkpoints.chapter_id 有外键约束）。"""
    repo = Repository(engine)
    repo.create_book(BOOK, "书")
    for i in range(1, 6):
        repo.create_chapter(f"ch{i}", BOOK, i, title=f"第{i}章")


def _fields(chapter_id: str, content: str = "内容", **extra: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "book_id": BOOK,
        "chapter_id": chapter_id,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "pipeline_version": "p1",
        "prompt_version": "v1",
        "compression_version": "",
        "schema_version": "v1",
    }
    fields.update(extra)
    return fields


def _task(chapter_id: str, content: str = "内容") -> ChapterTask:
    return ChapterTask(
        chapter_id=chapter_id,
        ordinal=int(chapter_id.replace("ch", "")),
        content=content,
        checkpoint_fields=_fields(chapter_id, content),
    )


def _make_provider(calls: list[str], *, fail: dict[str, str] | None = None):
    """确定性 fake provider；fail 模式：timeout/json/boom/once。"""
    fail = fail or {}

    async def process(task: ChapterTask) -> ProcessResult:
        calls.append(task.chapter_id)
        mode = fail.get(task.chapter_id)
        if mode == "timeout":
            await asyncio.sleep(10)
        elif mode == "json":
            raise NonRetryableError("无效 JSON：无法解析结构化输出")
        elif mode == "boom" and calls.count(task.chapter_id) == 1:
            raise RetryableError("瞬时网络错误")  # 第一次失败，重试成功
        elif mode == "once" and calls.count(task.chapter_id) == 1:
            await asyncio.sleep(10)  # 第一次超时，重试成功
        return ProcessResult(
            payload={"chapter_id": task.chapter_id, "ordinal": task.ordinal},
            usage=Usage(
                input_tokens=100,
                output_tokens=20,
                provider="fake",
                model="fake-model",
                profile_id="fp",
            ),
        )

    return process


async def _full_run(
    engine: Engine,
    tasks: list[ChapterTask],
    process_fn,
    *,
    concurrency: int = 2,
    timeout_seconds: float = 1.0,
    threshold: float = 0.0,
) -> tuple[str, object, list[str] | None]:
    mgr = RunManager(engine)
    run_id = mgr.create(BOOK, input_hash="ih1")
    assert mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
    runner = PipelineRunner(
        engine,
        run_id,
        BOOK,
        concurrency=concurrency,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0.01),
    )
    summary = await runner.run(tasks, process_fn, timeout_seconds=timeout_seconds)
    issues = finish_run(
        engine, run_id, total_chapters=len(tasks), summary=summary, error_ratio_threshold=threshold
    )
    return run_id, summary, issues


# ── 状态机 ──────────────────────────────────────────────────────


def test_run_state_machine(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    mgr = RunManager(migrated_db)
    run_id = mgr.create(BOOK)
    assert mgr.get(run_id)["status"] == RunStatus.CREATED.value

    assert mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
    assert mgr.transition(run_id, RunStatus.RUNNING, RunStatus.VALIDATING)
    assert mgr.transition(run_id, RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE)
    assert mgr.transition(run_id, RunStatus.READY_TO_ACTIVATE, RunStatus.ACTIVE)
    assert mgr.get(run_id)["status"] == RunStatus.ACTIVE.value

    # 非法转换失败（CAS）
    assert not mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
    # 重复激活新 run：旧 active → superseded（经 Activator 原子激活）
    from novelcanon.pipeline import Activator

    run2 = mgr.create(BOOK)
    mgr.transition(run2, RunStatus.CREATED, RunStatus.RUNNING)
    mgr.transition(run2, RunStatus.RUNNING, RunStatus.VALIDATING)
    mgr.transition(run2, RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE)
    assert Activator(migrated_db).activate(run2) is None
    assert mgr.get(run_id)["status"] == RunStatus.SUPERSEDED.value
    assert mgr.get(run2)["status"] == RunStatus.ACTIVE.value


# ── checkpoint 唯一键 ───────────────────────────────────────────


def test_checkpoint_key_sensitivity() -> None:
    base = _fields("ch1")
    key = checkpoint_key(base)
    for field in CHECKPOINT_FIELDS:
        changed = dict(base)
        changed[field] = "CHANGED"
        assert checkpoint_key(changed) != key, f"{field} 变化应使键失效"


# ── 正常全流程 + checkpoint 复用 ───────────────────────────────


def test_full_run_activates(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    tasks = [_task("ch1"), _task("ch2"), _task("ch3")]
    calls: list[str] = []

    async def main() -> None:
        run_id, summary, issues = await _full_run(migrated_db, tasks, _make_provider(calls))
        assert issues is None, issues
        assert summary.completed == 3 and summary.failed == 0
        assert RunManager(migrated_db).get(run_id)["status"] == RunStatus.ACTIVE.value
        assert calls == ["ch1", "ch2", "ch3"]

    asyncio.run(main())


def test_checkpoint_reuse_skips_provider(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    tasks = [_task("ch1", "相同内容"), _task("ch2")]
    calls: list[str] = []

    async def main() -> None:
        # run1：处理全部
        await _full_run(migrated_db, tasks, _make_provider(calls))
        assert calls == ["ch1", "ch2"]
        # run2：同键 → ch1 复用，ch2 复用；不调用 provider
        run2, summary, issues = await _full_run(migrated_db, tasks, _make_provider(calls))
        assert issues is None
        assert summary.reused == 2 and summary.completed == 2
        assert calls == ["ch1", "ch2"], "复用不得再次调用 provider"
        assert RunManager(migrated_db).get(run2)["status"] == RunStatus.ACTIVE.value

    asyncio.run(main())


# ── 断点续跑：只重跑未成功章节 ────────────────────────────────


def test_resume_only_pending(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    calls: list[str] = []

    async def main() -> None:
        # run1：ch1 成功，ch2 不可重试失败 → run1 failed
        tasks12 = [_task("ch1"), _task("ch2")]
        mgr = RunManager(migrated_db)
        run1 = mgr.create(BOOK)
        mgr.transition(run1, RunStatus.CREATED, RunStatus.RUNNING)
        runner = PipelineRunner(migrated_db, run1, BOOK)
        s1 = await runner.run(
            tasks12, _make_provider(calls, fail={"ch2": "json"}), timeout_seconds=1.0
        )
        assert s1.failed == 1 and s1.failed_chapters == ["ch2"]
        issues = finish_run(migrated_db, run1, total_chapters=2, summary=s1)
        assert issues and any("失败章节" in i for i in issues)
        assert mgr.get(run1)["status"] == RunStatus.FAILED.value

        # run2：全部 3 章；ch1 命中复用，ch2 重跑，ch3 新跑
        tasks3 = [_task("ch1"), _task("ch2"), _task("ch3")]
        run2, s2, issues2 = await _full_run(migrated_db, tasks3, _make_provider(calls))
        assert issues2 is None
        assert s2.reused == 1  # ch1 复用
        assert calls.count("ch1") == 1  # 只在 run1 调用过一次
        assert calls.count("ch2") == 2  # run1 失败 + run2 重跑
        assert calls.count("ch3") == 1
        assert mgr.get(run2)["status"] == RunStatus.ACTIVE.value

    asyncio.run(main())


# ── 重复投递幂等 ───────────────────────────────────────────────


def test_duplicate_delivery_idempotent(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    tasks = [_task("ch1")]
    calls: list[str] = []

    async def main() -> None:
        mgr = RunManager(migrated_db)
        run_id = mgr.create(BOOK)
        mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
        runner = PipelineRunner(migrated_db, run_id, BOOK)
        s1 = await runner.run(tasks, _make_provider(calls))
        s2 = await runner.run(tasks, _make_provider(calls))  # 同 run 重复投递
        assert s1.completed == 1
        assert s2.reused == 1  # 同键命中
        with migrated_db.connect() as conn:
            n = conn.execute(
                text("SELECT count(*) FROM run_checkpoints WHERE chapter_id = 'ch1'")
            ).scalar()
        assert n == 1, "重复投递不得产生重复 checkpoint"

    asyncio.run(main())


# ── writer 事务失败回滚 ────────────────────────────────────────


def test_writer_failure_rolls_back(migrated_db: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_book(migrated_db)
    tasks = [_task("ch1"), _task("ch2")]
    calls: list[str] = []

    async def main() -> None:
        mgr = RunManager(migrated_db)
        run_id = mgr.create(BOOK)
        mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)

        real_save = CheckpointService.save_with
        calls_to_save = 0

        def boom_save(self, conn, *args, **kwargs):  # noqa: ANN001
            nonlocal calls_to_save
            calls_to_save += 1
            if calls_to_save <= 2:
                raise RuntimeError("writer 事务失败（模拟）")
            return real_save(self, conn, *args, **kwargs)

        monkeypatch.setattr(CheckpointService, "save_with", boom_save)
        runner = PipelineRunner(migrated_db, run_id, BOOK, batch_size=1)
        s = await runner.run(tasks, _make_provider(calls), timeout_seconds=1.0)
        assert s.writer_failures >= 1
        # 失败的批没有留下 checkpoint（整批回滚）
        with migrated_db.connect() as conn:
            n = conn.execute(
                text("SELECT count(*) FROM run_checkpoints WHERE run_id = :r"), {"r": run_id}
            ).scalar()
        assert n == 0, "writer 失败不得留下部分 checkpoint"

    asyncio.run(main())


# ── 重试：超时后成功 / 不可重试直判失败 ──────────────────────


def test_retry_after_timeout(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    calls: list[str] = []

    async def main() -> None:
        mgr = RunManager(migrated_db)
        run_id = mgr.create(BOOK)
        mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
        runner = PipelineRunner(
            migrated_db,
            run_id,
            BOOK,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0.01),
        )
        s = await runner.run(
            [_task("ch1")],
            _make_provider(calls, fail={"ch1": "once"}),
            timeout_seconds=0.1,
        )
        assert s.completed == 1 and s.failed == 0
        assert calls == ["ch1", "ch1"], "第一次超时后应重试"

    asyncio.run(main())


def test_nonretryable_error_locates_chapter(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    tasks = [_task("ch1"), _task("ch2")]
    calls: list[str] = []

    async def main() -> None:
        run_id, summary, issues = await _full_run(
            migrated_db, tasks, _make_provider(calls, fail={"ch2": "json"})
        )
        assert summary.failed == 1
        assert summary.failed_chapters == ["ch2"]  # 可定位到章节
        assert issues and any("ch2" in i for i in issues)
        assert calls == ["ch1", "ch2"], "不可重试错误不得重试"

    asyncio.run(main())


# ── 旧 run 保持可查 ────────────────────────────────────────────


def test_old_run_remains_queryable(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    calls: list[str] = []

    async def main() -> None:
        # run1 全成功 → active
        run1, _, issues1 = await _full_run(migrated_db, [_task("ch1")], _make_provider(calls))
        assert issues1 is None
        # run2 有失败章 → failed；run1 保持 active
        run2, _, issues2 = await _full_run(
            migrated_db,
            [_task("ch1"), _task("ch2")],
            _make_provider(calls, fail={"ch2": "json"}),
        )
        assert issues2
        mgr = RunManager(migrated_db)
        assert mgr.get(run1)["status"] == RunStatus.ACTIVE.value
        assert mgr.get(run2)["status"] == RunStatus.FAILED.value

    asyncio.run(main())


def test_ledger_folds_provider_retry_meta_on_exhaustion(
    migrated_db: Engine,
) -> None:
    """验收 P1：provider 内部重试耗尽时（Usage 尚未构造），失败尝试数与
    消耗的 prompt token 由 client 附加到异常、runner 读入账本——失败调用
    也必须可审计。"""
    _ensure_book(migrated_db)
    tasks = [_task("ch1")]

    def provider_with_exhaustion():
        async def process(task: ChapterTask) -> ProcessResult:
            exc = RuntimeError("provider retries exhausted")
            exc.provider_retry_count = 3  # 模拟 GenerationClient 附加
            exc.provider_input_tokens = 150
            raise exc

        return process

    async def main() -> None:
        run_id, summary, issues = await _full_run(
            migrated_db, tasks, provider_with_exhaustion(), timeout_seconds=1.0
        )
        assert issues is not None
        assert summary.failed == 1
        led = TokenLedger(migrated_db).summary(run_id)
        # provider 内部 3 次失败尝试必须进入账本（runner 层重试另计）
        assert led["retry_count"] >= 3, f"provider 失败尝试必须入账：{led}"
        assert led["input_tokens"] >= 150, f"失败调用消耗的 token 必须入账：{led}"

    asyncio.run(main())


def test_ledger_sums_provider_internal_and_outer_retries(
    migrated_db: Engine,
) -> None:
    """验收 P1：成功后的账本 = result.usage（含 provider 内部 retry_count）
    + 此前外层失败尝试的累计 retry_usage——不再用 replace 覆盖，也不丢弃
    失败后成功的调用与 token。"""
    _ensure_book(migrated_db)
    tasks = [_task("ch1")]
    calls = {"n": 0}

    async def process(task: ChapterTask) -> ProcessResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableError("瞬时网络错误")  # 第一次外层失败
        return ProcessResult(
            payload={"chapter_id": task.chapter_id, "ordinal": task.ordinal},
            # 成功调用携带 provider 内部 retry_count=2（429 后成功）
            usage=Usage(
                input_tokens=100,
                output_tokens=20,
                retry_count=2,
                provider="fake",
                model="fake-model",
                profile_id="fp",
            ),
        )

    async def main() -> None:
        run_id, summary, issues = await _full_run(
            migrated_db, tasks, process, timeout_seconds=1.0
        )
        assert issues is None and summary.completed == 1
        led = TokenLedger(migrated_db).summary(run_id)
        # 总重试 = 外层 1 次 + provider 内部 2 次 = 3（不得覆盖为 1）
        assert led["retry_count"] == 3, f"provider 内部重试不得被外层覆盖：{led}"
        assert led["input_tokens"] == 100
        assert led["output_tokens"] == 20

    asyncio.run(main())


# ── Token 账本汇总 ─────────────────────────────────────────────


def test_token_ledger_summary(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    tasks = [_task("ch1"), _task("ch2"), _task("ch3")]
    calls: list[str] = []

    async def main() -> None:
        run_id, _, issues = await _full_run(migrated_db, tasks, _make_provider(calls))
        assert issues is None
        summary = TokenLedger(migrated_db).summary(run_id)
        # 每章 input=100 + output=20；3 章
        assert summary["input_tokens"] == 300
        assert summary["output_tokens"] == 60
        assert summary["total"] == 360
        assert summary["retry_count"] == 0

    asyncio.run(main())


# ── 故障注入收敛为单一 active run（04 退出标准）───────────────


def test_fault_injection_converges_to_single_active(migrated_db: Engine) -> None:
    _ensure_book(migrated_db)
    calls: list[str] = []

    async def main() -> None:
        # 3 章：ch1 首次超时（重试成功）、ch2 瞬时错误（重试成功）、ch3 正常
        tasks = [_task("ch1"), _task("ch2"), _task("ch3")]
        fail = {"ch1": "once", "ch2": "boom"}
        run_id, summary, issues = await _full_run(
            migrated_db, tasks, _make_provider(calls, fail=fail), timeout_seconds=0.1
        )
        assert issues is None
        assert summary.failed == 0 and summary.completed == 3

        mgr = RunManager(migrated_db)
        assert mgr.get(run_id)["status"] == RunStatus.ACTIVE.value
        # 同书仅一个 active
        with migrated_db.connect() as conn:
            n = conn.execute(
                text(
                    "SELECT count(*) FROM extraction_runs WHERE book_id = :b AND status = 'active'"
                ),
                {"b": BOOK},
            ).scalar()
        assert n == 1

    asyncio.run(main())


# ── 与真实导入衔接：fixture 书跑一遍流水线 ────────────────────


def test_pipeline_on_imported_book(imported_book) -> None:
    engine, book_id = imported_book
    repo = Repository(engine)
    chapters = repo.list_chapters(book_id)
    tasks = [
        ChapterTask(
            chapter_id=ch["chapter_id"],
            ordinal=ch["ordinal"],
            content=repo.get_book_text(book_id)[ch["char_start"] : ch["char_end"]],
            checkpoint_fields=_fields(ch["chapter_id"], ch["content_hash"] or "x"),
        )
        for ch in chapters
    ]
    # book_id 覆盖为实际 book
    for t in tasks:
        t.checkpoint_fields["book_id"] = book_id
    calls: list[str] = []

    async def main() -> None:
        mgr = RunManager(engine)
        run_id = mgr.create(book_id, input_hash="ih")
        mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
        runner = PipelineRunner(engine, run_id, book_id)
        s = await runner.run(tasks, _make_provider(calls))
        issues = finish_run(engine, run_id, total_chapters=len(tasks), summary=s)
        assert issues is None
        assert s.completed == len(chapters)

    asyncio.run(main())
