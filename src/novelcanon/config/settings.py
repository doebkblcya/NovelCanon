"""应用配置：加载、强校验与稳定 config hash（ADR-0003）。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from novelcanon.config.hash import stable_config_hash


class GenerationProfile(BaseModel):
    """generation 模型运行配置，版本化引用（定版方案 §13.1）。

    不绑定具体厂商/型号；密钥只从安全环境读取，绝不进入本模型。
    provider 为适配器名（openai-compatible 等）；api_key_env 指定存放密钥的
    环境变量名，密钥本身不落库、不进 config_hash。
    """

    profile_id: str
    context_window: int
    max_output_tokens: int
    structured_output_mode: str
    tokenizer_id: str
    provider: str = "openai-compatible"
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    concurrency_limit: int = 4
    requests_per_minute: int = 60
    tokens_per_minute: int = 0
    timeout_seconds: float = 60.0
    max_retries: int = 3
    retry_policy: str = "exponential"
    config_hash: str = ""

    @model_validator(mode="after")
    def _compute_hash(self) -> GenerationProfile:
        payload = self.model_dump(mode="json", exclude={"config_hash"})
        self.config_hash = stable_config_hash(payload)
        return self


class EmbeddingProfile(BaseModel):
    """embedding 模型运行配置（定版方案 §13.2）。"""

    profile_id: str
    tokenizer_id: str
    max_input_tokens: int
    vector_dimension: int
    normalization: str = "l2"
    distance_metric: str = "cosine"
    chunking_version: str = "v1"
    config_hash: str = ""

    @model_validator(mode="after")
    def _compute_hash(self) -> EmbeddingProfile:
        payload = self.model_dump(mode="json", exclude={"config_hash"})
        self.config_hash = stable_config_hash(payload)
        return self


class AppSettings(BaseSettings):
    """应用配置（pydantic-settings）。

    - 环境变量前缀 ``NOVELCANON_``，可选 ``.env`` 文件；
    - ``extra="forbid"``：未知字段直接启动失败，杜绝拼写错误静默通过；
    - 密钥只从安全环境读取，不写入配置快照、日志或数据库。
    """

    model_config = SettingsConfigDict(
        env_prefix="NOVELCANON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    db_path: Path = Path("data/novelcanon.db")
    books_dir: Path = Path("data/books")
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_json: bool = False

    # ── generation provider（阶段 06）─────────────────────────
    # 环境变量 LLM_* 与 NOVELCANON_LLM_* 均可；密钥字段 exclude=True，
    # 绝不进入 config_hash / 日志 / 数据库（只存内存供 GenerationClient）。
    llm_provider: str = "openai-compatible"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = Field(default="", exclude=True)
    llm_context_window: int = 8192
    llm_max_output: int = 2048
    llm_mode: str = "json_object"
    llm_tokenizer: str = "fake-v1"

    def config_hash(self) -> str:
        """整个应用配置的稳定 hash（密钥字段已 exclude，不进入 hash）。"""
        return stable_config_hash(self.model_dump(mode="json"))
