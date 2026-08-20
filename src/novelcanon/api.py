"""查询 API（阶段 11 P4，docs/implementation/11 §P4）。

- API 调用复用 CLI 的 application service（QueryExecutor），不在 API 层
  复制时间过滤业务逻辑；
- 请求必须包含明确 book_id；支持 knowledge cutoff 与 world at chapter；
- 返回事实、证据、章节与运行版本（context_id/route/cutoff + active run /
  index / profile 版本）；
- 超时、分页、限流、审计日志与稳定错误码（统一 invalid_params 形状）。

启动：uvicorn novelcanon.api:app（或 novelcanon serve --port 8000）。

执行模型（阶段 11 复审 P0/P1/P2，锁定环境多轮验证）：
- **所有端点一律 async def**——FastAPI 对同步 `def` 端点会经
  anyio.to_thread 在线程池执行（锁定环境的 anyio 路径阻塞），async
  端点由事件循环直接 await，完全不经过 anyio 线程机制；
- **同步 executor（默认 QueryExecutor）提交到 ThreadPoolExecutor 后台
  执行**（隔离事件循环：慢检索/embedding 卡住时不阻塞 /health 与其他
  请求），等待采用**轮询 `future.done()`**（concurrent.futures 线程安全
  状态，不依赖 asyncio.wrap_future 的 call_soon_threadsafe 完成通知——
  该通知在锁定环境不可靠，曾导致正常查询 30s 后才 408）；轮询间隙
  `asyncio.sleep` 让出事件循环，deadline 超时返回稳定 408 timeout；
  **有界准入**：执行中查询达到池容量（4）时新请求立即返回 503
  overloaded，防止超时任务占满池后正常查询排队（复审 P1）；池随
  FastAPI lifespan 关闭；
- **异步 executor（executor_factory 注入）经 asyncio.wait_for 超时保护**
  ——asyncio 原生可取消，超时返回稳定 408 timeout；
- **embedding 后端按 profile 应用级缓存**（httpx 连接池复用，不随每次
  查询重建；adapter 实现 close，lifespan 关闭时统一释放，复审 P1）。
"""

from __future__ import annotations

import asyncio
import inspect
import queue
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from fastapi import Query as FQuery
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from novelcanon.config.settings import AppSettings
from novelcanon.query import QueryExecutor, route_question
from novelcanon.retrieval.factory import UnknownEmbeddingProfileError
from novelcanon.retrieval.vectorstore import (
    BruteForceVectorStore,
    Embedder,
    FakeEmbedder,
    VectorStore,
)

API_VERSION = "api-v1"

# 默认请求超时（秒）；超时返回稳定错误码 timeout（408）
DEFAULT_REQUEST_TIMEOUT = 30.0

# 同步 executor 后台执行池容量（复审 P1：有界准入——卡住的查询占满池后，
# 新请求立即返回饱和错误 503，而不是无限排队后全部 408）。
_POOL_MAX_WORKERS = 4

# 同步 executor 后台执行时的完成轮询间隔（秒）——轮询 future.done()
# 不依赖事件循环线程调度通知；间隙让出事件循环，保证 /health 可响应。
_POLL_INTERVAL = 0.05

# 空闲 worker 的队列轮询间隔（秒）——shutdown 后 worker 在间隔内退出。
_WORKER_IDLE_POLL = 1.0


class _DaemonQueryPool:
    """自管 daemon 工作线程池（复审 P1/P2）。

    **不能用 ThreadPoolExecutor**：CPython 3.13/3.14 的 worker 非 daemon，
    且 ``concurrent.futures.thread`` 注册了 atexit ``_python_exit``——无论
    worker 是否 daemon，解释器退出时都会显式 join 所有线程；运行中的
    卡死任务会**阻止进程关闭**（实测 daemon 化后子进程仍被 join 拖死）。

    本池自管 daemon 线程（threading.Thread(daemon=True) + queue.Queue，
    **不注册 concurrent.futures 的 atexit join**）：解释器退出时 daemon
    线程被直接终止，卡死任务永不阻塞进程退出。对外仍返回
    ``concurrent.futures.Future``（done()/result()/add_done_callback/
    cancel 语义与 ThreadPoolExecutor 一致，轮询与 done callback 逻辑
    不变）。

    - 运行中任务无法强制中断：资源回收由**严格下游超时**保障
      （embedding adapter / LLM client 均有有限超时，线程最终自然结束）；
    - shutdown(wait=False, cancel_futures=True)：取消**排队未开始**的
      任务，唤醒 worker 退出；运行中任务继续执行到下游超时为止。
    """

    def __init__(self, max_workers: int) -> None:
        self._max_workers = max_workers
        self._queue: queue.Queue[tuple[Future, Callable, tuple, dict] | None] = queue.Queue()
        self._shutdown = False
        # 关闭竞态防护（复审 P1）：submit 的「检查 _shutdown + 入队」与
        # shutdown 的「置 _shutdown + 清队列 + 哨兵入队」必须原子——否则
        # shutdown 先清空队列并放入哨兵、submit 随后把任务排在哨兵之后，
        # worker 全部退出后任务永不执行也永不取消（Future 悬挂）。
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        for _ in range(max_workers):
            t = threading.Thread(target=self._run, name="dsh-query-worker", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, fn: Callable, /, *args: object, **kwargs: object) -> Future:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future: Future = Future()
            self._queue.put((future, fn, args, kwargs))
        return future

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=_WORKER_IDLE_POLL)
            except queue.Empty:
                if self._shutdown:
                    return
                continue
            if item is None:
                return
            future, fn, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue  # 已被 cancel：不执行
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 —— Future 承载任务异常
                future.set_exception(exc)

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = False) -> None:
        """取消排队任务并唤醒 worker 退出；运行中任务继续（daemon，不
        阻塞进程退出）。wait 参数保留以兼容 ThreadPoolExecutor 语义
        （本池 daemon 线程无需 join）。

        与 submit 同一把锁：关闭后不再接受新任务（submit 抛
        RuntimeError），杜绝「任务排在退出哨兵之后」的悬挂 Future。
        """
        del wait
        with self._lock:
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()
            for _ in range(self._max_workers):
                self._queue.put(None)


# ── 请求 / 响应模型 ───────────────────────────────────────────


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    book_id: str | None = Field(default=None, min_length=1)  # 缺失在端点显式报 missing_book
    knowledge_cutoff: int | None = Field(default=None, ge=0)
    world_at: int | None = Field(default=None, ge=0)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class QueryResponse(BaseModel):
    answer: str
    route: str
    query_type: str
    sources: list[dict]
    confidence: float
    caveats: list[str]
    context_id: str
    knowledge_cutoff: int | None
    world_at: int | None
    cannot_answer: bool
    page: int
    page_size: int
    total_sources: int
    # 运行版本（P4：返回事实、证据、章节与运行版本）
    run_version: str | None = None
    index_version: str | None = None
    profile: str = ""
    api_version: str = API_VERSION


# ── 限流（每 book 滑动窗口，进程内）────────────────────────────


@dataclass
class RateLimiter:
    """简单内存限流：每 book 固定窗口内最多 N 次请求。"""

    max_requests: int = 60
    window_seconds: float = 60.0
    _calls: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._calls[key]
        while q and now - q[0] > self.window_seconds:
            q.popleft()
        if len(q) >= self.max_requests:
            return False
        q.append(now)
        return True


# ── 错误码 ────────────────────────────────────────────────────

ERR = {
    "missing_book": "book_id 必须提供",
    "book_not_found": "book 不存在",
    "rate_limited": "请求频率超限",
    "invalid_params": "参数无效",
    "timeout": "请求超时",
    "overloaded": "查询后端过载（执行池饱和），请稍后重试",
    "backend_not_configured": "embedding 后端未配置（服务端配置错误）",
    "internal": "内部错误",
}


def _error(status: int, code: str, detail: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": detail or ERR[code]})


# ── 应用工厂 ──────────────────────────────────────────────────


def create_app(
    engine: Engine | None = None,
    *,
    rate_limit: RateLimiter | None = None,
    synthesis_client: object | None = None,
    profile_id: str = "",
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    executor_factory: Callable[[str], QueryExecutor] | None = None,
) -> FastAPI:
    """创建查询 API 应用（engine 缺省时按 AppSettings 打开）。

    request_timeout：单请求执行超时（秒）；executor_factory：测试注入
    用（返回 QueryExecutor 或等接口的桩）。
    """
    from contextlib import asynccontextmanager

    import structlog

    logger = structlog.get_logger("novelcanon.api")
    # 生产 embedding 后端：按应用配置（NOVELCANON_EMBEDDING_*）启动注册
    # （幂等；未配置时 no-op，仅 fake-embed-v<N> 可用）
    from novelcanon.retrieval.factory import register_configured_backends

    register_configured_backends()
    limiter = rate_limit or RateLimiter()
    _engine = engine
    # 同步 executor 的后台执行池（复审 P0：隔离事件循环 + 可超时；
    # 复审 P1：有界准入 + lifespan 清理；复审 P2：自管 daemon worker——
    # 不注册 concurrent.futures atexit join，卡死任务不阻止进程退出）。
    # 等待用轮询 future.done()，不依赖 wrap_future 的完成通知。
    _pool = _DaemonQueryPool(max_workers=_POOL_MAX_WORKERS)
    # 正在执行的同步查询数（含超时后仍在后台运行的线程）——达到容量即
    # 饱和拒绝（503），防止卡住任务占满池后正常查询无限排队。
    _in_flight = 0
    # 应用级 embedding 后端缓存（复审 P1）：按 profile 复用 embedder/
    # client（httpx 连接池），不随每次查询重建；lifespan 关闭时统一释放。
    _backend_cache: dict[str, tuple[Embedder, VectorStore]] = {}

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        """FastAPI 生命周期：应用关闭时释放生产资源（复审 P1/P2）。

        - 关闭 embedding 后端（adapter 实现 close → httpx.Client 关闭，
          连接/文件描述符不泄漏）；
        - 关闭查询执行池：cancel_futures 取消排队任务；正在运行的任务
          无法中断，但 (a) 下游调用携带严格超时（线程最终自然结束）且
          (b) worker 已 daemon 化（_DaemonThreadPoolExecutor）——即使
          极端卡死也不阻塞解释器退出（进程可正常关闭）。
        """
        try:
            yield
        finally:
            for embedder, _store in _backend_cache.values():
                closer = getattr(embedder, "close", None)
                if closer is not None:
                    closer()
            _backend_cache.clear()
            _pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="NovelCanon 查询 API", version=API_VERSION, lifespan=_lifespan)
    # 测试/运维可检查执行池状态（如 lifespan 清理后的关闭标记）
    app.state.executor_pool = _pool

    def _release_slot(_future: object) -> None:
        """future done callback：任务**在线程中实际完成**时释放准入计数。

        concurrent.futures 的回调在完成该 future 的线程内执行（GIL 保护，
        不经过事件循环调度）——即使请求早已因超时返回 408，后台线程结束
        时计数也会递减，饱和池自动恢复（复审 P1）。
        """
        nonlocal _in_flight
        _in_flight -= 1

    def _open_engine() -> Engine:
        nonlocal _engine
        if _engine is None:
            from novelcanon.storage.engine import create_db_engine
            from novelcanon.storage.migrations import migrate_to_head

            settings = AppSettings()
            migrate_to_head(settings.db_path)
            _engine = create_db_engine(settings.db_path)
        return _engine

    def _default_executor(book_id: str) -> QueryExecutor:
        eng = _open_engine()
        embedder, vector_store = _runtime_backend(eng, book_id)
        return QueryExecutor(
            eng,
            book_id,
            embedder=embedder,
            vector_store=vector_store,
            synthesis_client=synthesis_client,
            profile_id=profile_id,
        )

    def _runtime_backend(eng: Engine, book_id: str) -> tuple[Embedder, VectorStore]:
        """按 active index 的 embedding profile 创建运行时检索后端（P1）。

        经 retrieval.factory 的可插拔工厂：profile 注册表内置
        fake-embed-v<N>（测试/fixture），真实 profile 通过
        register_backend 注册 adapter。不再硬编码 FakeEmbedder(8)——
        profile 与索引不一致时检索层（_verify_profile）拒绝查询。
        测试通过 executor_factory 注入 fake。

        复审 P1：backend 按 profile **缓存于应用级**（_backend_cache）——
        不随每次查询重新 create_backend（避免每个请求新建 httpx.Client
        连接池，泄漏连接/文件描述符且无法复用）；lifespan 关闭时统一
        close。
        """
        from novelcanon.retrieval import create_backend
        from novelcanon.retrieval.indexer import get_active_index_version

        index = get_active_index_version(eng, book_id)
        profile = (index or {}).get("embedding_profile_id")
        if not profile:
            # 无 active 索引：结构化查询可跑；raw-detail/hybrid 由检索层
            # 报「无 active 索引」（与 CLI 一致）
            return FakeEmbedder(dimension=8), BruteForceVectorStore(dimension=8)
        if profile not in _backend_cache:
            _backend_cache[profile] = create_backend(profile)
        return _backend_cache[profile]

    def _executor(book_id: str) -> QueryExecutor:
        return (executor_factory or _default_executor)(book_id)

    def _run_versions(book_id: str) -> tuple[str | None, str | None]:
        """active run + active index 版本（P4 运行版本返回）。"""
        eng = _open_engine()
        run_version: str | None = None
        index_version: str | None = None
        with eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT run_id FROM extraction_runs"
                    " WHERE book_id = :b AND status = 'active' ORDER BY rowid"
                ),
                {"b": book_id},
            ).fetchone()
            if row is not None:
                run_version = row[0]
        from novelcanon.retrieval.indexer import get_active_index_version

        index = get_active_index_version(eng, book_id)
        if index is not None:
            index_version = index["index_version_id"]
        return run_version, index_version

    # 统一 pydantic 校验错误（P4：稳定错误码，不暴露 FastAPI 默认结构）
    # async：exception handler 若为同步函数，starlette 同样经 anyio.to_thread
    # 执行（复审 P0：与端点一致，必须 async 才能完全绕开 anyio 线程路径）
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> object:
        from fastapi.responses import JSONResponse

        errors = [
            {"loc": list(e.get("loc", [])), "msg": str(e.get("msg", ""))} for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "invalid_params",
                    "message": ERR["invalid_params"],
                    "errors": errors,
                }
            },
        )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "api_version": API_VERSION}

    async def _execute_query(req: QueryRequest) -> QueryResponse:
        # 准入计数读写（同步分支）与 _release_slot 共享 create_app 作用域
        nonlocal _in_flight
        started = time.perf_counter()
        if not req.book_id:
            raise _error(400, "missing_book")
        if not limiter.allow(req.book_id):
            raise _error(429, "rate_limited")
        eng = _open_engine()
        with eng.connect() as conn:
            book = conn.execute(
                text("SELECT book_id FROM books WHERE book_id = :b"),
                {"b": req.book_id},
            ).fetchone()
        if book is None:
            raise _error(404, "book_not_found")
        try:
            executor = _executor(req.book_id)
            ask = executor.ask
            if inspect.iscoroutinefunction(ask):
                # 异步 executor（可取消）：asyncio.wait_for 超时保护（408）。
                # asyncio 原生等待，不经过任何线程调度/完成通知。
                try:
                    result = await asyncio.wait_for(
                        ask(
                            req.question,
                            knowledge_cutoff=req.knowledge_cutoff,
                            world_at=req.world_at,
                        ),
                        timeout=request_timeout,
                    )
                except TimeoutError:
                    raise _error(408, "timeout") from None
            else:
                # 同步 executor（默认 QueryExecutor）：提交到线程池后台执行
                # ——慢检索/embedding 卡住时不阻塞事件循环（/health 与其他
                # 请求可响应，复审 P0）。等待采用**轮询 future.done()**：
                # concurrent.futures 线程安全状态，不依赖 asyncio.wrap_future
                # 的 call_soon_threadsafe 完成通知（该通知在锁定环境不可靠，
                # 曾导致正常查询 30s 后才 408）；轮询间隙 asyncio.sleep 让出
                # 事件循环，deadline 到期返回稳定 408 timeout。
                #
                # 复审 P1：future.cancel() 无法停止**已开始运行**的线程——
                # 超时任务仍占用 worker。因此用**有界准入**：正在执行的
                # 查询达到池容量时，新请求立即返回饱和错误 503 overloaded
                # （不排队）；准入计数由 **done callback** 释放——任务在线程
                # 中实际完成时（即使客户端早已收到 408），回调在线程内执行
                # 并递减计数，池自动恢复；下游网络调用自身携带超时
                # （embedding/LLM adapter 均有），线程最终会结束。
                if _in_flight >= _POOL_MAX_WORKERS:
                    logger.warning(
                        "query_pool_saturated",
                        book_id=req.book_id,
                        in_flight=_in_flight,
                        max_workers=_POOL_MAX_WORKERS,
                    )
                    raise _error(503, "overloaded")
                future = _pool.submit(
                    ask,
                    req.question,
                    knowledge_cutoff=req.knowledge_cutoff,
                    world_at=req.world_at,
                )
                _in_flight += 1
                future.add_done_callback(_release_slot)
                deadline = time.monotonic() + request_timeout
                while not future.done():
                    if time.monotonic() >= deadline:
                        future.cancel()
                        raise _error(408, "timeout")
                    await asyncio.sleep(_POLL_INTERVAL)
                result = future.result()
        except UnknownEmbeddingProfileError as exc:
            # 索引声明了未注册的 embedding profile：服务端配置错误（500
            # backend_not_configured），不是用户参数问题（复审 P1）。
            # UnknownEmbeddingProfileError 继承 RuntimeError，不会被下面
            # ValueError 分支误判为 400 invalid_params。
            logger.error("embedding_profile_unconfigured", book_id=req.book_id, error=str(exc))
            raise _error(500, "backend_not_configured", str(exc)) from exc
        except ValueError as exc:
            raise _error(400, "invalid_params", str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 —— API 边界统一错误码
            logger.error("query_failed", book_id=req.book_id, error=str(exc))
            raise _error(500, "internal") from exc

        payload = result.answer
        all_sources = payload.get("sources") or []
        total = len(all_sources)
        start = (req.page - 1) * req.page_size
        page_sources = all_sources[start : start + req.page_size]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        run_version, index_version = _run_versions(req.book_id)
        logger.info(
            "query_served",
            book_id=req.book_id,
            route=payload.get("route"),
            query_type=payload.get("query_type"),
            sources=total,
            latency_ms=elapsed_ms,
            cached=result.cached,
        )
        return QueryResponse(
            answer=payload.get("answer", ""),
            route=payload.get("route", ""),
            query_type=payload.get("query_type", ""),
            sources=page_sources,
            confidence=payload.get("confidence", 0.0),
            caveats=payload.get("caveats", []),
            context_id=payload.get("context_id", ""),
            knowledge_cutoff=payload.get("knowledge_cutoff"),
            world_at=payload.get("world_at"),
            cannot_answer=payload.get("cannot_answer", False),
            page=req.page,
            page_size=req.page_size,
            total_sources=total,
            run_version=run_version,
            index_version=index_version,
            profile=payload.get("query_profile", ""),
        )

    @app.post("/query", response_model=QueryResponse)
    async def query(req: QueryRequest) -> QueryResponse:
        """结构化查询 / 混合检索 / 证据接地问答（与 CLI 同一 service）。"""
        return await _execute_query(req)

    @app.get("/query", response_model=QueryResponse)
    async def query_get(
        question: str = FQuery(min_length=1),
        book_id: str | None = FQuery(default=None, min_length=1),
        knowledge_cutoff: int | None = FQuery(default=None, ge=0),
        world_at: int | None = FQuery(default=None, ge=0),
    ) -> QueryResponse:
        """GET 便捷接口：同 POST /query（book_id 必须明确）。"""
        return await _execute_query(
            QueryRequest(
                question=question,
                book_id=book_id,
                knowledge_cutoff=knowledge_cutoff,
                world_at=world_at,
            )
        )

    @app.get("/explain")
    async def explain(question: str = FQuery(min_length=1)) -> dict:
        """路由 explain（验证实际命中路线）。"""
        decision = route_question(question)
        return {
            "query_type": decision.query_type,
            "route": decision.route,
            "matched_keywords": decision.matched_keywords,
            "is_fallback": decision.is_fallback,
            "explain": decision.explain,
        }

    return app


app = create_app()
