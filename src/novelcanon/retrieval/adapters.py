"""生产 embedding adapter（阶段 11 复审 P1）。

OpenAICompatEmbedder：OpenAI 兼容 ``/embeddings`` HTTP 端点适配器
（httpx 同步客户端），l2 归一化（与检索层余弦一致），返回向量维数与
配置严格校验。

- 配置驱动：``EmbeddingProfile``（profile_id / vector_dimension /
  normalization）+ base_url / model / api_key（密钥只从安全环境读取，
  不落库、不进日志）；
- 经 ``retrieval.factory.register_backend`` 注册后，API 与索引共用同一
  工厂：``create_backend(profile_id)`` 按 active index 的
  embedding_profile_id 创建运行时后端（不再硬编码 FakeEmbedder）。

集成测试用本地 HTTP 端点（tests/test_api.py）验证真实网络路径。
"""

from __future__ import annotations

import math
from collections.abc import Callable

import httpx

from novelcanon.config.settings import EmbeddingProfile
from novelcanon.retrieval.vectorstore import Embedder, VectorStore


class OpenAICompatEmbedder:
    """OpenAI 兼容 embeddings API 适配器（Embedder Protocol 实现）。

    生命周期（复审 P1）：未注入 client 时本类**拥有**内部 httpx.Client
    （连接池），实现 close()/上下文管理器——API 按 profile 应用级缓存
    backend，应用关闭（lifespan）时统一 close，连接/文件描述符不泄漏。
    注入的 client（外部拥有）不被 close。
    """

    def __init__(
        self,
        profile: EmbeddingProfile,
        *,
        base_url: str,
        model: str = "",
        api_key: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        if profile.vector_dimension < 1:
            raise ValueError(
                f"embedding profile {profile.profile_id!r} 缺少合法 vector_dimension"
                "（NOVELCANON_EMBEDDING_DIMENSION），生产后端必须声明维度"
            )
        self.profile_id = profile.profile_id
        self.dimension = profile.vector_dimension
        self._model = model or profile.profile_id
        self._normalize = profile.normalization in ("", "l2")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._owns_client = client is None
        self._client = client or httpx.Client(headers=headers, timeout=httpx.Timeout(30.0))
        self._url = f"{base_url.rstrip('/')}/embeddings"

    def close(self) -> None:
        """释放自有的 httpx 连接池（外部注入的 client 由外部管理）。"""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAICompatEmbedder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def embed(self, text: str) -> list[float]:
        """调用 /embeddings 并校验维数 + l2 归一化。"""
        resp = self._client.post(self._url, json={"model": self._model, "input": text})
        resp.raise_for_status()
        data = resp.json()
        try:
            vec = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:  # noqa: PERF203
            raise ValueError(
                f"embedding 响应缺少 data[0].embedding（profile {self.profile_id}）"
            ) from exc
        if len(vec) != self.dimension:
            raise ValueError(
                f"embedding 返回维数 {len(vec)} != 配置 {self.dimension}"
                f"（profile {self.profile_id}）——索引与后端配置不一致"
            )
        if self._normalize:
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            return [v / norm for v in vec]
        return list(vec)


def build_openai_compat_factory(
    profile: EmbeddingProfile,
    *,
    base_url: str,
    model: str = "",
    api_key: str = "",
) -> Callable[[], tuple[Embedder, VectorStore]]:
    """构造 OpenAI 兼容后端的注册工厂（profile_id → (embedder, store)）。

    向量存储用 BruteForceVectorStore（embedding_records 全扫描余弦），
    与 FakeEmbedder 基线同一实现，仅替换 embedder 为生产 adapter。
    """

    def _build() -> tuple[Embedder, VectorStore]:
        from novelcanon.retrieval.vectorstore import BruteForceVectorStore

        embedder = OpenAICompatEmbedder(profile, base_url=base_url, model=model, api_key=api_key)
        return embedder, BruteForceVectorStore(dimension=profile.vector_dimension)

    return _build
