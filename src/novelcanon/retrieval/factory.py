"""embedder / vector store 运行时工厂（阶段 11 复审 P1）。

按 active index 声明的 embedding_profile_id 创建生产检索后端：
- 内置 fake-embed-v<N>（测试与 fixture Pilot 用）；
- 真实 profile 通过 EMBEDDER_FACTORIES 注册表扩展（接入真实 embedding
  模型时注册 adapter 即可，API/索引共用同一工厂）；
- 应用启动时经 register_configured_backends 从 NOVELCANON_EMBEDDING_*
  配置注册 OpenAI 兼容生产 adapter（配置驱动，非仅扩展点）。

API 不再硬编码 FakeEmbedder(8)：profile 与索引不一致时检索层
（retrieval.service._verify_profile）会拒绝查询。

UnknownEmbeddingProfileError 继承 RuntimeError（非 ValueError）：
服务端配置错误不会被 API 的 ValueError 分支误判为 400 invalid_params。
"""

from __future__ import annotations

import re
from collections.abc import Callable

from novelcanon.retrieval.vectorstore import (
    BruteForceVectorStore,
    Embedder,
    FakeEmbedder,
    VectorStore,
)

# profile_id → (embedder 工厂, vector store 工厂)
# 扩展点：注册真实 embedding adapter（如 openai-text-embedding-3-small）。
EMBEDDER_FACTORIES: dict[str, Callable[[], tuple[Embedder, VectorStore]]] = {}


def _fake_factory(profile_id: str) -> Callable[[], tuple[Embedder, VectorStore]]:
    m = re.match(r"^fake-embed-v(\d+)$", profile_id)
    assert m, f"非 fake profile：{profile_id!r}"

    def _build() -> tuple[Embedder, VectorStore]:
        dim = int(m.group(1))
        return FakeEmbedder(dimension=dim), BruteForceVectorStore(dimension=dim)

    return _build


class UnknownEmbeddingProfileError(RuntimeError):
    """active index 声明的 embedding profile 未注册生产 adapter。

    RuntimeError（非 ValueError）：服务端配置错误，API 映射为稳定
    500 backend_not_configured，不得落入 400 invalid_params。
    """


def register_backend(profile_id: str, factory: Callable[[], tuple[Embedder, VectorStore]]) -> None:
    """注册生产后端（扩展点）：profile_id → embedder/vector store 工厂。"""
    EMBEDDER_FACTORIES[profile_id] = factory


def unregister_backend(profile_id: str) -> None:
    """注销后端（测试清理用；幂等）。"""
    EMBEDDER_FACTORIES.pop(profile_id, None)


def register_configured_backends(settings: object | None = None) -> list[str]:
    """按应用配置注册生产 embedding 后端（应用启动时调用，幂等）。

    环境启用 ``NOVELCANON_EMBEDDING_PROFILE_ID`` 时，从配置构造
    EmbeddingProfile + OpenAI 兼容 HTTP adapter 并注册进工厂；
    未配置（默认）则 no-op——仅 fake-embed-v<N> 可用（测试/fixture）。
    返回本次注册的 profile_id 列表。

    配置契约（密钥只从安全环境读取）：
    - NOVELCANON_EMBEDDING_PROFILE_ID：必填（启用生产 embedding 的开关）；
    - NOVELCANON_EMBEDDING_BASE_URL：必填（/embeddings 端点）；
    - NOVELCANON_EMBEDDING_MODEL：缺省 = profile_id；
    - NOVELCANON_EMBEDDING_DIMENSION：必填（索引与查询后端一致的前提）。
    """
    from novelcanon.config.settings import AppSettings, EmbeddingProfile
    from novelcanon.retrieval.adapters import build_openai_compat_factory

    settings = settings or AppSettings()
    profile_id = getattr(settings, "embedding_profile_id", "") or ""
    if not profile_id:
        return []
    dimension = int(getattr(settings, "embedding_dimension", 0) or 0)
    if dimension < 1:
        raise ValueError(
            f"embedding profile {profile_id!r} 缺少 NOVELCANON_EMBEDDING_DIMENSION"
            "（生产后端必须声明向量维数，索引与查询共用）"
        )
    base_url = str(getattr(settings, "embedding_base_url", "") or "")
    if not base_url:
        raise ValueError(f"embedding profile {profile_id!r} 缺少 NOVELCANON_EMBEDDING_BASE_URL")
    profile = EmbeddingProfile(
        profile_id=profile_id,
        tokenizer_id="fake-v1",
        max_input_tokens=8192,
        vector_dimension=dimension,
        normalization="l2",
        distance_metric="cosine",
        chunking_version="v1",
    )
    register_backend(
        profile_id,
        build_openai_compat_factory(
            profile,
            base_url=base_url,
            model=str(getattr(settings, "embedding_model", "") or ""),
            api_key=str(getattr(settings, "embedding_api_key", "") or ""),
        ),
    )
    return [profile_id]


def create_backend(profile_id: str) -> tuple[Embedder, VectorStore]:
    """按 profile 创建运行时后端；未注册的 profile 抛清晰错误。"""
    if profile_id in EMBEDDER_FACTORIES:
        return EMBEDDER_FACTORIES[profile_id]()
    if re.match(r"^fake-embed-v\d+$", profile_id):
        return _fake_factory(profile_id)()
    raise UnknownEmbeddingProfileError(
        f"未注册的 embedding profile {profile_id!r}：请通过"
        " novelcanon.retrieval.factory.register_backend 注册生产 adapter"
        "（内置 fake-embed-v<N> 仅用于测试与 fixture Pilot）"
    )
