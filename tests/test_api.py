"""阶段 11 查询 API 测试（docs/implementation/11 §P4）。

覆盖验证项：
- 请求必须包含明确 book_id（缺失/不存在 → 稳定错误码）；
- 支持 knowledge cutoff 与 world at chapter；
- 返回事实、证据、章节与运行版本；
- 分页、限流、审计日志；
- API 与 CLI/QueryExecutor 对相同查询返回一致事实集（11 验证项）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from sqlalchemy import Engine

from novelcanon.api import RateLimiter, create_app
from novelcanon.query import QueryExecutor
from novelcanon.retrieval import BruteForceVectorStore, FakeEmbedder
from tests.helpers import seed_active_book


class SyncASGIClient:
    """不依赖 anyio blocking portal 的同步 ASGI 测试客户端（P0）。

    starlette TestClient 经 anyio blocking portal 驱动 ASGI 应用，在锁定
    环境（fastapi 0.115/0.141 × starlette 0.46/1.6 × httpx 0.28 × anyio
    4.14 均验证）的 /health 请求永久阻塞（线程栈停在 portal）。本客户端
    每次请求用 asyncio.run 创建全新事件循环 + httpx.ASGITransport，不经
    portal：确定性可退出，任何环境下都能结束 pytest。
    """

    def __init__(self, app) -> None:
        self._app = app

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        async def _run() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_run())

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)


def make_client(app) -> SyncASGIClient:
    """测试用客户端工厂：避免 TestClient portal 死锁。"""
    return SyncASGIClient(app)


def test_query_requires_book_id(tmp_path: Path, migrated_db: Engine) -> None:
    app = create_app(migrated_db)
    client = make_client(app)
    # 缺 book_id → 显式 missing_book 错误码（不再走 pydantic 默认 422）
    r = client.post("/query", json={"question": "萧炎的修为"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "missing_book"
    r2 = client.post("/query", json={"question": "萧炎修为", "book_id": "book_nonexistent"})
    assert r2.status_code == 404
    body = r2.json()["detail"]
    assert body["code"] == "book_not_found"


def test_query_returns_run_versions(tmp_path: Path, migrated_db: Engine) -> None:
    """P4：返回 active run / index / profile 版本。"""
    data = seed_active_book(migrated_db, tmp_path)
    app = create_app(migrated_db)
    client = make_client(app)
    r = client.post(
        "/query",
        json={"question": "萧炎的修为状态", "book_id": data["book_id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["run_version"] == data["run_id"]
    assert body["profile"]  # query_profile 非空
    assert "index_version" in body  # 无 active 索引时可为 null


def test_query_timeout(tmp_path: Path, migrated_db: Engine) -> None:
    """P4：异步 executor 请求超时返回稳定错误码 timeout（408）。

    asyncio.wait_for 可取消；默认同步 QueryExecutor 的超时见
    test_query_sync_slow_executor_times_out。
    """
    data = seed_active_book(migrated_db, tmp_path)

    class SlowExecutor:
        async def ask(self, *args, **kwargs):  # noqa: ANN002, ANN003
            await asyncio.sleep(5)
            raise AssertionError("不应到达这里")

    app = create_app(
        migrated_db,
        request_timeout=0.05,
        executor_factory=lambda book_id: SlowExecutor(),  # type: ignore[return-value]
    )
    client = make_client(app)
    r = client.post("/query", json={"question": "萧炎的修为状态", "book_id": data["book_id"]})
    assert r.status_code == 408
    assert r.json()["detail"]["code"] == "timeout"


def test_query_sync_slow_executor_times_out(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P0：**同步** executor 的慢查询也必须返回 408（线程池后台执行 +
    轮询超时）——request_timeout 对生产同步路径有效。"""
    import time

    data = seed_active_book(migrated_db, tmp_path)

    class SlowSyncExecutor:
        def ask(self, *args, **kwargs):  # noqa: ANN002, ANN003
            time.sleep(5)
            raise AssertionError("不应到达这里")

    app = create_app(
        migrated_db,
        request_timeout=0.05,
        executor_factory=lambda book_id: SlowSyncExecutor(),  # type: ignore[return-value]
    )
    client = make_client(app)
    r = client.post("/query", json={"question": "萧炎的修为状态", "book_id": data["book_id"]})
    assert r.status_code == 408
    assert r.json()["detail"]["code"] == "timeout"


async def test_health_responsive_during_slow_query(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P0：慢查询占用线程池期间，/health 等事件循环请求仍即时响应。

    同步 executor 在 ThreadPoolExecutor 后台执行，事件循环不被阻塞——
    慢检索/embedding 卡住时 API 仍可服务健康检查与其他请求。
    """
    import time

    data = seed_active_book(migrated_db, tmp_path)

    class SlowSyncExecutor:
        def ask(self, *args, **kwargs):  # noqa: ANN002, ANN003
            time.sleep(5)
            raise AssertionError("不应到达这里")

    app = create_app(
        migrated_db,
        request_timeout=1.0,
        executor_factory=lambda book_id: SlowSyncExecutor(),  # type: ignore[return-value]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slow = asyncio.create_task(
            client.post("/query", json={"question": "萧炎的修为状态", "book_id": data["book_id"]})
        )
        await asyncio.sleep(0.2)  # 慢查询已提交到线程池并开始轮询
        health = await client.get("/health")
        assert health.status_code == 200, "慢查询期间 /health 必须仍响应"
        resp = await slow
        assert resp.status_code == 408
        assert resp.json()["detail"]["code"] == "timeout"


def test_query_normal_query_does_not_timeout(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P0：正常查询不得被超时误伤（同步 executor 线程池 + 轮询 done()）。

    默认 QueryExecutor（同步 ask）提交后台执行、轮询线程安全完成状态
    （不依赖 wrap_future 完成通知）——即使收紧 request_timeout，正常查询
    也必须及时返回 200（锁定环境完成通知不可靠曾导致 30s 后 408）。
    """
    data = seed_active_book(migrated_db, tmp_path)
    app = create_app(migrated_db, request_timeout=1.0)  # 正常查询应远快于 1s
    client = make_client(app)
    r = client.post("/query", json={"question": "萧炎的修为状态", "book_id": data["book_id"]})
    assert r.status_code == 200, r.text
    assert r.json()["answer"]


def test_validation_error_unified_code(tmp_path: Path, migrated_db: Engine) -> None:
    """P4：pydantic 校验错误统一为 invalid_params 形状。"""
    app = create_app(migrated_db)
    client = make_client(app)
    r = client.post(
        "/query",
        json={"question": "问" * 501, "book_id": "b"},  # 超长 question
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_params"
    assert "errors" in detail
    assert isinstance(detail["errors"], list)


def test_query_endpoint_matches_executor(tmp_path: Path, migrated_db: Engine) -> None:
    """API 与 QueryExecutor 对相同查询返回一致事实集（11 验证项）。"""
    data = seed_active_book(migrated_db, tmp_path)
    app = create_app(migrated_db)
    client = make_client(app)
    r = client.post(
        "/query",
        json={"question": "萧炎的修为状态", "book_id": data["book_id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["route"] == "structured"
    assert body["answer"]
    assert body["context_id"]
    assert body["api_version"] == "api-v1"

    # 与直接 QueryExecutor 一致性：sources 的章节集合相同
    executor = QueryExecutor(
        migrated_db,
        data["book_id"],
        embedder=FakeEmbedder(dimension=8),
        vector_store=BruteForceVectorStore(dimension=8),
    )
    direct = executor.ask("萧炎的修为状态").answer
    api_ordinals = {s["observed_ordinal"] for s in body["sources"] if s.get("observed_ordinal")}
    direct_ordinals = {
        s["observed_ordinal"] for s in direct["sources"] if s.get("observed_ordinal")
    }
    assert api_ordinals == direct_ordinals, (
        f"API 与 CLI 应返回一致章节：{api_ordinals} vs {direct_ordinals}"
    )


def test_query_cutoff_and_world(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    app = create_app(migrated_db)
    client = make_client(app)
    # world_at=0：ch2 披露（from=1）的 alive 状态不可见
    r = client.post(
        "/query",
        json={
            "question": "萧炎的状态如何",
            "book_id": data["book_id"],
            "world_at": 0,
        },
    )
    assert r.status_code == 200
    assert "alive" not in r.json()["answer"]


def test_query_pagination(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    app = create_app(migrated_db)
    client = make_client(app)
    r = client.post(
        "/query",
        json={
            "question": "萧炎的修为状态",
            "book_id": data["book_id"],
            "page": 1,
            "page_size": 1,
        },
    )
    body = r.json()
    assert len(body["sources"]) == 1
    assert body["total_sources"] >= 1
    assert body["page"] == 1


def test_rate_limiting(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    limiter = RateLimiter(max_requests=2, window_seconds=60)
    app = create_app(migrated_db, rate_limit=limiter)
    client = make_client(app)
    for _ in range(2):
        r = client.post("/query", json={"question": "萧炎修为", "book_id": data["book_id"]})
        assert r.status_code == 200
    r3 = client.post("/query", json={"question": "萧炎修为", "book_id": data["book_id"]})
    assert r3.status_code == 429
    assert r3.json()["detail"]["code"] == "rate_limited"


def test_explain_endpoint(tmp_path: Path, migrated_db: Engine) -> None:
    app = create_app(migrated_db)
    client = make_client(app)
    r = client.get("/explain", params={"question": "萧炎与纳兰嫣然的关系"})
    assert r.status_code == 200
    body = r.json()
    assert body["query_type"] == "relation"
    assert body["route"] == "structured"


def test_cutoff_leakage_scan(tmp_path: Path, migrated_db: Engine) -> None:
    """P5：cutoff 泄露扫描——来源章节不得超过 cutoff。"""
    data = seed_active_book(migrated_db, tmp_path)
    from novelcanon.ops import scan_cutoff_leakage

    result = scan_cutoff_leakage(
        migrated_db,
        data["book_id"],
        questions=["萧炎的修为状态", "萧炎所在家族"],
        cutoffs=[0, 1, 2],
    )
    assert result.checks == 6
    assert not result.leaked, f"不应有泄露：{result.as_dict()}"


def test_api_backend_matches_active_index_profile(tmp_path: Path, migrated_db: Engine) -> None:
    """P1：API 检索后端按 active index 的 embedding profile 创建。

    非默认维度（fake-embed-v16）索引下，hybrid 查询必须成功——
    profile 校验不匹配时会抛错（retrieval.service._verify_profile）。
    """
    from novelcanon.retrieval import (
        BruteForceVectorStore,
        FakeEmbedder,
        FakeTokenizer,
        build_index,
    )

    data = seed_active_book(migrated_db, tmp_path)
    build_index(
        migrated_db,
        data["book_id"],
        tokenizer=FakeTokenizer(),
        embedder=FakeEmbedder(16),
        vector_store=BruteForceVectorStore(16),
    )
    app = create_app(migrated_db)
    client = make_client(app)
    # 无结构化关键词 → fallback raw_detail → hybrid 检索（需要索引+profile）
    r = client.post(
        "/query",
        json={"question": "萧炎在乌坦城经历的细节", "book_id": data["book_id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["route"] == "hybrid"


def test_api_unconfigured_profile_returns_backend_not_configured(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """复审 P1：active index 声明未注册的 embedding profile → 稳定 500
    backend_not_configured（服务端配置错误，不是 400 invalid_params）。"""
    from novelcanon.retrieval import (
        BruteForceVectorStore,
        FakeEmbedder,
        FakeTokenizer,
        build_index,
    )

    data = seed_active_book(migrated_db, tmp_path)
    embedder = FakeEmbedder(16)
    embedder.profile_id = "openai-text-embedding-3-large"  # 未注册的生产 profile
    build_index(
        migrated_db,
        data["book_id"],
        tokenizer=FakeTokenizer(),
        embedder=embedder,
        vector_store=BruteForceVectorStore(16),
    )
    app = create_app(migrated_db)
    client = make_client(app)
    r = client.post(
        "/query",
        json={"question": "萧炎在乌坦城经历的细节", "book_id": data["book_id"]},
    )
    assert r.status_code == 500, r.text
    assert r.json()["detail"]["code"] == "backend_not_configured", r.json()


def test_api_production_embedding_adapter_integration(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：配置驱动的真实 embedding adapter（OpenAI 兼容 /embeddings
    HTTP 端点）注册进工厂 → 建索引 + API hybrid 查询全链路可用。

    复审 P1（资源生命周期）：API 按 profile **应用级缓存** backend——
    多次查询复用同一 embedder/httpx 连接池（服务端连接数不增长），
    lifespan 关闭时统一 close。
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from novelcanon.config.settings import EmbeddingProfile
    from novelcanon.retrieval import (
        BruteForceVectorStore,
        FakeTokenizer,
        build_index,
        register_backend,
        unregister_backend,
    )
    from novelcanon.retrieval.adapters import OpenAICompatEmbedder

    PROFILE = "openai-text-embedding-3-small"
    DIM = 16

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive：连接复用可观测
        connections = 0

        def setup(self) -> None:
            _Handler.connections += 1
            super().setup()

        def do_POST(self) -> None:  # noqa: N802 —— http.server 回调命名
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = json.dumps({"data": [{"embedding": [0.1] * DIM}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # noqa: ANN002 —— 静音测试日志
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = EmbeddingProfile(
            profile_id=PROFILE,
            tokenizer_id="fake-v1",
            max_input_tokens=512,
            vector_dimension=DIM,
        )
        base_url = f"http://127.0.0.1:{server.server_port}"

        def factory():
            return (
                OpenAICompatEmbedder(profile, base_url=base_url, model="test-embed"),
                BruteForceVectorStore(DIM),
            )

        register_backend(PROFILE, factory)
        data = seed_active_book(migrated_db, tmp_path)
        embedder = OpenAICompatEmbedder(profile, base_url=base_url, model="test-embed")
        build_index(
            migrated_db,
            data["book_id"],
            tokenizer=FakeTokenizer(),
            embedder=embedder,
            vector_store=BruteForceVectorStore(DIM),
        )
        app = create_app(migrated_db)
        client = make_client(app)
        r = client.post(
            "/query",
            json={"question": "萧炎在乌坦城经历的细节", "book_id": data["book_id"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["route"] == "hybrid"
        assert r.json()["profile"], "响应应带 profile"
        # 第二次查询：复用同一应用级 backend/httpx 连接池（HTTP/1.1
        # keep-alive）——总连接数 = 索引构建客户端 1 + API 缓存客户端 1
        # = 2；若每次查询重建 client，第二次查询会增长到 3。
        r2 = client.post(
            "/query",
            json={"question": "萧炎在乌坦城经历的细节", "book_id": data["book_id"]},
        )
        assert r2.status_code == 200, r2.text
        assert _Handler.connections == 2, (
            f"API 查询必须复用应用级缓存 client（连接池复用）：{_Handler.connections}"
        )
        # lifespan 关闭：统一 close 缓存 backend（自有 httpx client 释放）。
        # __aenter__/__aexit__ 同一事件循环（跨循环会被 shutdown_asyncgens
        # 提前关闭 generator，finally 误执行）。
        import asyncio

        async def _close_lifespan() -> None:
            ctx = app.router.lifespan_context(app)
            await ctx.__aenter__()
            await ctx.__aexit__(None, None, None)

        asyncio.run(_close_lifespan())
    finally:
        server.shutdown()
        thread.join()
        unregister_backend(PROFILE)


def test_query_pool_saturation_returns_overloaded_and_recovers(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """复审 P1：卡住查询占满线程池后，新请求返回饱和错误 503 overloaded
    （不排队），卡住任务结束后池自动恢复、正常查询可继续 200。"""
    import time

    data = seed_active_book(migrated_db, tmp_path)

    class _FlakyExecutor:
        """前 4 次调用卡住（模拟慢检索），之后正常回答（恢复验证）。

        实例计数跨请求共享（factory 返回同一实例）——前 4 次调用卡住，
        之后恢复正常，用于验证「超时后池恢复」。
        """

        def __init__(self) -> None:
            self.calls = 0

        def ask(self, *args, **kwargs):  # noqa: ANN002, ANN003
            from types import SimpleNamespace

            self.calls += 1
            if self.calls <= 4:
                time.sleep(5)  # 卡住：占满 4 个 worker
            return SimpleNamespace(
                answer={
                    "answer": "ok",
                    "sources": [],
                    "route": "structured",
                    "query_type": "entity_state",
                    "confidence": 1.0,
                    "caveats": [],
                    "context_id": "ctx-sat",
                    "knowledge_cutoff": None,
                    "world_at": None,
                    "cannot_answer": False,
                    "query_profile": "v1:test",
                },
                cached=False,
            )

    flaky = _FlakyExecutor()
    app = create_app(
        migrated_db,
        request_timeout=0.3,
        executor_factory=lambda book_id: flaky,  # type: ignore[return-value]
    )
    client = make_client(app)
    # 前 4 个请求：卡住 → 各自 408（后台线程仍占用 worker）
    for _ in range(4):
        r = client.post("/query", json={"question": "q", "book_id": data["book_id"]})
        assert r.status_code == 408, r.text
    # 第 5 个：池已饱和（4 个线程仍在运行）→ 立即 503，而非排队 408
    r5 = client.post("/query", json={"question": "q", "book_id": data["book_id"]})
    assert r5.status_code == 503, r5.text
    assert r5.json()["detail"]["code"] == "overloaded", r5.json()
    # 等待卡住任务结束（5s sleep）→ 池恢复 → 正常查询 200
    time.sleep(5.2)
    r_ok = client.post("/query", json={"question": "q", "book_id": data["book_id"]})
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["answer"] == "ok"


def test_api_lifespan_shuts_down_executor_pool(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：FastAPI lifespan 关闭时执行池显式 shutdown（不再接受新
    任务），缓存 embedding 后端统一 close。"""
    import asyncio

    import pytest

    app = create_app(migrated_db)

    async def _lifecycle() -> None:
        # __aenter__ 与 __aexit__ 必须在同一事件循环（asyncio.run 结束时
        # shutdown_asyncgens 会提前关闭挂起的 lifespan generator，导致
        # finally 提前执行——跨循环进出会误关池）
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        pool = app.state.executor_pool
        # 运行中可提交
        f = pool.submit(lambda: 1)
        assert f.result() == 1
        await ctx.__aexit__(None, None, None)
        # 关闭后：池标记关闭，提交新任务抛 RuntimeError（无泄漏线程）
        with pytest.raises(RuntimeError):
            pool.submit(lambda: 1)

    asyncio.run(_lifecycle())


def test_openai_embedder_close_lifecycle() -> None:
    """复审 P1：OpenAICompatEmbedder 生命周期——自有 httpx.Client 由
    close()/上下文管理器释放（连接池不泄漏）；外部注入的 client 由
    外部管理，不被 close。"""
    import httpx

    from novelcanon.config.settings import EmbeddingProfile
    from novelcanon.retrieval.adapters import OpenAICompatEmbedder

    profile = EmbeddingProfile(
        profile_id="openai-text-embedding-3-small",
        tokenizer_id="fake-v1",
        max_input_tokens=512,
        vector_dimension=16,
    )
    # 自有 client：close() 释放
    own = OpenAICompatEmbedder(profile, base_url="http://127.0.0.1:1", model="m")
    assert own._owns_client is True
    assert not own._client.is_closed
    own.close()
    assert own._client.is_closed, "自有 client 必须被 close 释放"
    # 上下文管理器同样释放
    with OpenAICompatEmbedder(profile, base_url="http://127.0.0.1:1", model="m") as cm:
        assert not cm._client.is_closed
    assert cm._client.is_closed
    # 外部注入 client：不被 close（外部拥有）
    external = httpx.Client()
    injected = OpenAICompatEmbedder(
        profile, base_url="http://127.0.0.1:1", model="m", client=external
    )
    assert injected._owns_client is False
    injected.close()
    assert not external.is_closed, "外部注入的 client 不应被 adapter close"
    external.close()


def test_process_exits_with_stuck_pool_task() -> None:
    """复审 P1/P2：存在运行中卡死任务时，lifespan 关闭后进程仍可在限定
    时间内退出。

    CPython 3.13/3.14 ThreadPoolExecutor 的 worker 是**非 daemon**——
    卡死任务会在解释器退出时被 join、阻止进程关闭。api 使用
    _DaemonThreadPoolExecutor（worker daemon=True）后，运行中任务不再
    阻塞退出。本测试在**子进程**中验证：提交永不结束的任务 → lifespan
    关闭 → 进程在 30s 内正常退出（非 daemon 行为会在 timeout 处被杀）。
    """
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import asyncio
        import time

        from novelcanon.api import create_app

        class Stuck:
            def ask(self, *args, **kwargs):  # noqa: ANN002, ANN003
                time.sleep(300)  # 卡死：永不返回（模拟下游彻底卡住）
                raise AssertionError("不应到达这里")

        app = create_app(None, request_timeout=0.1, executor_factory=lambda b: Stuck())

        async def main() -> None:
            ctx = app.router.lifespan_context(app)
            await ctx.__aenter__()
            pool = app.state.executor_pool
            # 启动一个**运行中**的卡死任务（模拟超时后仍在后台跑的查询）
            pool.submit(time.sleep, 300)
            await asyncio.sleep(0.2)  # 确保任务已进入 worker 线程运行
            await ctx.__aexit__(None, None, None)  # lifespan 关闭（不阻塞）
            print("LIFESPAN_DONE")

        asyncio.run(main())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,  # 非 daemon 行为会因 join 卡死任务而在这里被杀
    )
    assert proc.returncode == 0, proc.stderr
    assert "LIFESPAN_DONE" in proc.stdout, proc.stdout


def test_pool_submit_shutdown_race_no_hanging_futures() -> None:
    """复审 P1：并发 submit/shutdown 无关闭竞态——不得产生永不完成的
    Future。

    高并发下反复 submit（多线程）与 shutdown 交错：所有被 submit 接受
    的 Future 必须全部完成（执行返回结果或被取消），绝不允许任务排在
    退出哨兵之后悬挂。shutdown 后 submit 抛 RuntimeError 是合法拒绝。
    """
    import threading
    import time

    from novelcanon.api import _DaemonQueryPool

    def _submitter(pool, stop, futures, unexpected) -> None:
        while not stop.is_set():
            try:
                f = pool.submit(lambda: 1)
                futures.append(f)
            except RuntimeError:
                pass  # shutdown 后拒绝——合法行为
            except Exception as exc:  # noqa: BLE001 —— 其他异常视为竞态缺陷
                unexpected.append(exc)

    for _round in range(50):
        pool = _DaemonQueryPool(max_workers=2)
        stop = threading.Event()
        futures: list = []
        unexpected: list = []
        threads = [
            threading.Thread(target=_submitter, args=(pool, stop, futures, unexpected))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        time.sleep(0.005)  # 让 submit 与 shutdown 高频交错
        pool.shutdown(wait=False, cancel_futures=True)
        stop.set()
        for t in threads:
            t.join()

        assert not unexpected, f"round={_round} 出现异常：{unexpected}"
        # 所有已接受的 Future 必须完成（执行返回结果或被取消）——悬挂即竞态。
        # 注：被 shutdown 取消的 Future 状态为 CANCELLED（f.done()=True），
        # 但 concurrent.futures.wait 只把 CANCELLED_AND_NOTIFIED/FINISHED 计入
        # done——用 f.done() 逐个判定（与 ThreadPoolExecutor 语义一致）。
        hanging = [f for f in futures if not f.done()]
        assert not hanging, (
            f"round={_round} 存在永不完成的 Future（submit/shutdown 竞态）：{len(hanging)} 个悬挂"
        )
        for f in futures:
            if f.cancelled():
                continue
            assert f.result() == 1, f"round={_round} 执行结果异常"


def test_books_list_endpoint(tmp_path: Path, migrated_db: Engine) -> None:
    """阶段 11 复审 D：GET /books 返回图书列表（前端选择页一次拿全）。"""
    data = seed_active_book(migrated_db, tmp_path)
    app = create_app(migrated_db)
    client = make_client(app)
    r = client.get("/books")
    assert r.status_code == 200
    books = r.json()
    assert isinstance(books, list) and books, "至少应有一本书"
    b = next(x for x in books if x["book_id"] == data["book_id"])
    assert b["title"]
    assert b["chapter_count"] == 3
    assert b["active_run"] == data["run_id"]
    assert "active_index" in b and "embedding_profile" in b  # 未建索引时可为 null


def test_query_sources_carry_span_text(tmp_path: Path, migrated_db: Engine) -> None:
    """阶段 11 复审 D：/query 的 sources 回填 span_text（前端点击证据展开）。"""
    from novelcanon.storage.repository import Repository

    data = seed_active_book(migrated_db, tmp_path)
    app = create_app(migrated_db)
    client = make_client(app)
    r = client.post(
        "/query",
        json={"question": "萧炎的修为状态", "book_id": data["book_id"]},
    )
    assert r.status_code == 200, r.text
    sources = r.json()["sources"]
    assert sources, "应有 sources"
    full = Repository(migrated_db).get_book_text(data["book_id"])
    chapter_bounds = {
        c["chapter_id"]: (c["char_start"], c["char_end"])
        for c in Repository(migrated_db).list_chapters(data["book_id"])
    }
    located = [s for s in sources if s.get("span_text") is not None]
    assert located, "至少一个 source 应带 span_text"
    for s in located:
        start, end = chapter_bounds[s["chapter_id"]]
        cs, ce = s["char_start"], s["char_end"]
        expect = full[start + cs : start + ce]
        assert s["span_text"] == expect, "span_text 必须等于原文切片"
