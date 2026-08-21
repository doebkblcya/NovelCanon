"""生成客户端工厂（阶段二 04：查询 LLM 合成接线）。

build_synthesis_client：按应用配置构造查询合成用的 GenerationClient
（OpenAI 兼容 / JSON 输出 / 独立 profile "query-synth"）；未配置 LLM
时返回 None——调用方（CLI/API）缺省走确定性合成，不改变既有行为。

GenerationClient.complete(prompt) 返回 GenerationResult(raw_text,
usage)，与 query.synthesis._Client Protocol 完全匹配，可直接注入
SynthesisService，无需适配器。
"""

from __future__ import annotations

from novelcanon.config.hash import stable_config_hash
from novelcanon.config.settings import AppSettings, GenerationProfile
from novelcanon.generation.client import GenerationClient
from novelcanon.query.synthesis import SYNTHESIS_SCHEMA_VERSION
from novelcanon.retrieval.tokenizer import FakeTokenizer

QUERY_SYNTH_PROFILE_PREFIX = "query-synth"


def _synthesis_profile_id(settings: AppSettings) -> str:
    """稳定合成 profile：模型 + base_url + 输出模式 + prompt schema hash。

    复审 P1：固定 "query-synth" 会在模型/配置变化时仍命中旧缓存——
    profile 必须编码生成配置，缓存键（synthesis_profile）按此隔离。
    """
    digest = stable_config_hash(
        {
            "model": settings.llm_model,
            "base_url": settings.llm_base_url,
            "mode": "json",
            "schema": SYNTHESIS_SCHEMA_VERSION,
            "max_output_tokens": settings.llm_max_output,
        }
    )
    return f"{QUERY_SYNTH_PROFILE_PREFIX}:{digest[:12]}"


def build_synthesis_client(
    settings: AppSettings | None = None,
) -> GenerationClient | None:
    """生产查询合成 client；未配置 LLM → None（确定性合成）。

    复用与 Map 相同的 LLM 配置（NOVELCANON_LLM_*），但强制 JSON 输出
    模式与稳定 profile（模型/配置 hash 编码，缓存键按 profile 隔离）。
    """
    settings = settings or AppSettings()
    if not settings.llm_model:
        return None
    profile = GenerationProfile(
        profile_id=_synthesis_profile_id(settings),
        context_window=settings.llm_context_window,
        max_output_tokens=settings.llm_max_output,
        structured_output_mode="json",  # 合成 prompt 要求输出 JSON
        tokenizer_id=settings.llm_tokenizer,
        provider=settings.llm_provider,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key_env="NOVELCANON_LLM_API_KEY",
        concurrency_limit=1,  # 查询合成单请求，无需并发
        requests_per_minute=60,
        timeout_seconds=30.0,
        max_retries=2,
        retry_policy="exponential",
    )
    return GenerationClient(
        profile, tokenizer=FakeTokenizer(), api_key=settings.llm_api_key or None
    )
