"""阶段二 04：查询 LLM 合成接线测试。

覆盖：
- build_synthesis_client：未配置 LLM → None（确定性合成，行为不变）；
- 配置 LLM → GenerationClient，稳定 profile（模型/配置 hash，复审 P1：
  固定 profile 会命中旧缓存）+ JSON 输出；
- GenerationClient 公开 profile_id（缓存键 synthesis_profile 非空）；
- GenerationClient.aclose 幂等（重复调用不抛错）；
- API 无 LLM 配置时查询仍确定性（synthesized=False，sprofile=deterministic）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from novelcanon.api import create_app
from novelcanon.config.settings import AppSettings
from novelcanon.generation.factory import QUERY_SYNTH_PROFILE_PREFIX, build_synthesis_client
from tests.helpers import seed_active_book
from tests.test_api import make_client


def _settings_without_llm() -> AppSettings:
    """显式无 LLM 的配置（覆盖 .env 的真实 LLM 配置）。"""
    return AppSettings(llm_model="", llm_base_url="", llm_api_key="")


def test_build_synthesis_client_none_without_llm() -> None:
    """未配置 LLM → None（API/CLI 缺省走确定性合成）。"""
    assert build_synthesis_client(_settings_without_llm()) is None


def test_build_synthesis_client_with_llm() -> None:
    """配置 LLM → GenerationClient，稳定 profile（含模型/配置 hash）+ JSON 模式。"""
    settings = AppSettings(
        llm_model="test-model", llm_base_url="https://example.test/v1", llm_api_key="sk-test"
    )
    client = build_synthesis_client(settings)
    assert client is not None
    assert client.profile_id.startswith(QUERY_SYNTH_PROFILE_PREFIX)
    assert client._profile.structured_output_mode == "json"  # noqa: SLF001


def test_profile_changes_with_model() -> None:
    """模型变化 → 合成 profile 变化（缓存键隔离，复审 P1）。"""
    base = AppSettings(llm_model="m1", llm_base_url="https://example.test/v1", llm_api_key="")
    other = AppSettings(llm_model="m2", llm_base_url="https://example.test/v1", llm_api_key="")
    c1 = build_synthesis_client(base)
    c2 = build_synthesis_client(other)
    assert c1 is not None and c2 is not None
    assert c1.profile_id != c2.profile_id


def test_generation_client_aclose_idempotent() -> None:
    """aclose 幂等：重复调用不抛错（API lifespan 多次关闭安全）。"""
    settings = AppSettings(llm_model="test-model", llm_base_url="https://example.test/v1")
    client = build_synthesis_client(settings)
    assert client is not None

    import asyncio

    async def _close_twice():
        await client.aclose()
        await client.aclose()

    asyncio.run(_close_twice())


def test_api_query_deterministic_without_llm(
    tmp_path: Path, migrated_db: Engine, monkeypatch
) -> None:
    """无 LLM 配置时 API 查询仍确定性合成（接线不改变既有行为）。"""
    monkeypatch.setattr("novelcanon.generation.factory.AppSettings", _settings_without_llm)
    data = seed_active_book(migrated_db, tmp_path)
    app = create_app(migrated_db)
    client = make_client(app)
    r = client.post("/query", json={"question": "萧炎的修为状态", "book_id": data["book_id"]})
    assert r.status_code == 200
    body = r.json()
    assert "确定性合成" in body["answer"]
    assert body["caveats"]  # 含「未做模型推断」
