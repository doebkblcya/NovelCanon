"""Generation provider 适配器（阶段 06，docs/implementation/06 §2/§4）。

- GenerationClient：OpenAI 兼容 chat completions 的 httpx 异步客户端，
  tenacity 指数退避重试传输错误与限流/服务端错误（06 §4）；
  API key 只从安全环境读取（profile.api_key_env），绝不落库/进请求 hash；
  请求 hash 可审计但不泄露密钥（不含 key）。
- FakeGenerationClient：无网络确定性实现（测试/开发），按 prompt 内容
  返回给定 JSON，等价于人工标注的「完美 LLM」。

usage 优先取响应 usage 字段，缺失时用 tokenizer 计量（token 账本全覆盖）。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from novelcanon.config.hash import stable_config_hash
from novelcanon.config.settings import GenerationProfile
from novelcanon.pipeline.ledger import Usage
from novelcanon.retrieval.tokenizer import Tokenizer

_RETRYABLE_STATUS = (408, 409, 429, 500, 502, 503, 504)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


@dataclass(frozen=True)
class GenerationResult:
    """一次模型调用的原始输出与计量。"""

    raw_text: str
    usage: Usage


def resolve_api_key(profile: GenerationProfile) -> str | None:
    """从安全环境读取 API key（不落库、不进入配置快照/日志/请求 hash）。"""
    if not profile.api_key_env:
        return None
    return os.environ.get(profile.api_key_env)


def request_hash(prompt: str, *, model: str = "", profile_id: str = "") -> str:
    """可审计但不泄露密钥的请求 hash（不含 API key）。"""
    return stable_config_hash(
        {"prompt": prompt, "model": model, "profile_id": profile_id}
    )


def response_hash(raw_text: str) -> str:
    return stable_config_hash({"response": raw_text})


class GenerationClient:
    """OpenAI 兼容 provider 适配器（httpx + tenacity）。"""

    def __init__(
        self,
        profile: GenerationProfile,
        *,
        api_key: str | None = None,
        tokenizer: Tokenizer | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._profile = profile
        self._api_key = api_key if api_key is not None else resolve_api_key(profile)
        self._tokenizer = tokenizer
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(profile.timeout_seconds)
        )
        self._call = self._make_retrying_call()
        # 重试审计（P1 修复）：失败尝试计数 + 锁（并发段请求共享 client）
        import threading

        self._ledger_lock = threading.Lock()
        self._retry_attempts = 0

    def _make_retrying_call(self) -> Callable[[str], Awaitable[httpx.Response]]:
        profile = self._profile

        async def call(prompt: str) -> httpx.Response:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            body: dict[str, object] = {
                "model": profile.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": profile.max_output_tokens,
            }
            if profile.structured_output_mode in ("json", "json_object"):
                body["response_format"] = {"type": "json_object"}
            retrying = AsyncRetrying(
                reraise=True,
                stop=stop_after_attempt(max(1, profile.max_retries)),
                wait=wait_random_exponential(multiplier=0.5, max=8.0),
                retry=retry_if_exception(_is_retryable),
            )
            attempt = 0
            async for attempt_state in retrying:
                attempt = attempt_state.retry_state.attempt_number
                with attempt_state:
                    try:
                        response = await self._client.post(
                            f"{profile.base_url.rstrip('/')}/chat/completions",
                            headers=headers,
                            json=body,
                        )
                        response.raise_for_status()
                        return response
                    except Exception:  # noqa: BLE001
                        # 每次失败尝试都计入账本（P1 修复：provider 内部
                        # 重试不再对 runner 不可见）
                        self._record_retry_attempt(attempt)
                        raise
            raise RuntimeError("unreachable: retrying loop exhausted")

        return call

    def _record_retry_attempt(self, attempt: int) -> None:
        """记录一次失败尝试（供账本统计）。

        每次失败的 HTTP 尝试都计数；最终 usage.retry_count = 成功前的
        失败尝试数（重试次数 = 总尝试 - 成功的那次）。
        """
        with self._ledger_lock:
            self._retry_attempts += 1

    async def complete(self, prompt: str) -> GenerationResult:
        """调用 provider 并解析原始输出 + usage。

        传输/限流/服务端错误在 client 内部重试（指数退避），每次失败
        尝试与最终成功调用都可通过 Usage.retry_count 审计（P1 修复）。
        """
        response = await self._call(prompt)
        data = response.json()
        content = ""
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
        usage = self._usage_from(data, prompt, content)
        # 把本次调用经历的失败尝试并入 usage（runner 记账时可见）
        with self._ledger_lock:
            retries = self._retry_attempts
            self._retry_attempts = 0
        if retries:
            usage = Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                retry_count=usage.retry_count + retries,
                discarded_tokens=usage.discarded_tokens,
                provider=usage.provider,
                model=usage.model,
            )
        return GenerationResult(raw_text=content, usage=usage)

    def _usage_from(self, data: dict, prompt: str, content: str) -> Usage:
        raw = data.get("usage") or {}
        profile = self._profile
        if self._tokenizer is not None:
            input_tokens = int(raw.get("prompt_tokens") or self._tokenizer.count(prompt))
            output_tokens = int(
                raw.get("completion_tokens") or self._tokenizer.count(content)
            )
        else:
            input_tokens = int(raw.get("prompt_tokens") or 0)
            output_tokens = int(raw.get("completion_tokens") or 0)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider=profile.provider,
            model=profile.model,
            profile_id=profile.profile_id,
        )


class FakeGenerationClient:
    """无网络确定性 provider（测试/开发）：按 prompt 内容返回给定 JSON。

    respond 可以是 {prompt 片段: JSON 文本} 映射，或接收完整 prompt 返回
    JSON 文本的函数（支持 repair：根据是否含「上次输出不符合要求」分支）。
    """

    def __init__(
        self,
        respond: Mapping[str, str] | Callable[[str], str],
        *,
        usage: Usage | None = None,
    ) -> None:
        self._respond = respond
        self._usage = usage or Usage(
            input_tokens=100, output_tokens=40, provider="fake", model="fake-model"
        )
        self.calls: list[str] = []  # 记录每次请求的 prompt（测试断言用）

    async def complete(self, prompt: str) -> GenerationResult:
        self.calls.append(prompt)
        if isinstance(self._respond, Mapping):
            raw = next(
                (v for k, v in self._respond.items() if k in prompt), "{}"
            )
        else:
            raw = self._respond(prompt)
        return GenerationResult(raw_text=raw, usage=self._usage)
