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

import dataclasses
import os
from collections.abc import Callable, Mapping
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
    return stable_config_hash({"prompt": prompt, "model": model, "profile_id": profile_id})


def response_hash(raw_text: str) -> str:
    return stable_config_hash({"response": raw_text})


def attach_retry_meta(
    exc: BaseException,
    failures: int,
    prompt: str,
    tokenizer: Tokenizer | None,
) -> None:
    """把 provider 内部重试的累计计量附加到最终异常（P1：失败也入账）。

    - provider_retry_count：本次调用失败的尝试次数（含最终失败）；
    - provider_input_tokens：失败尝试消耗的 prompt token 估计
      （tokenizer 计量 × 失败次数；限流/服务端错误通常不返回 usage）。

    runner 在异常路径读取并并入账本；附加失败（异常类型不可变）不阻塞
    原异常抛出。
    """
    try:
        exc.provider_retry_count = int(failures)  # type: ignore[attr-defined]
        est = 0
        if failures and tokenizer is not None:
            try:
                est = int(tokenizer.count(prompt)) * failures
            except Exception:  # noqa: BLE001 —— 计量失败不影响审计主数据
                est = 0
        exc.provider_input_tokens = est  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 —— 附加失败不吞原异常
        pass


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

    async def complete(self, prompt: str) -> GenerationResult:
        """调用 provider 并解析原始输出 + usage。

        传输/限流/服务端错误在本次调用内重试（指数退避），每次失败尝试
        计数并并入 Usage.retry_count。

        P1 修复：失败计数是**每次 complete 调用的局部变量**——多段 Map
        并发请求共享同一个 GenerationClient 时，A 请求的重试不会被先成功
        的 B 请求消费（此前挂在实例上的共享计数器会串账）。
        """
        profile = self._profile
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
        failures = 0  # 本次调用内失败的尝试数（成功前的重试次数）
        try:
            async for attempt_state in retrying:
                with attempt_state:
                    try:
                        response = await self._client.post(
                            f"{profile.base_url.rstrip('/')}/chat/completions",
                            headers=headers,
                            json=body,
                        )
                        response.raise_for_status()
                        break
                    except Exception:  # noqa: BLE001
                        failures += 1
                        raise
        except BaseException as exc:  # noqa: BLE001 —— 重试耗尽/传输错误
            # P1 修复：重试耗尽时（所有尝试都失败，Usage 尚未构造）内部
            # 尝试次数与失败调用消耗的 prompt token 也要可审计——把累计
            # 数据附加到最终异常，runner 记账时读取并入账本。
            attach_retry_meta(exc, failures, prompt, self._tokenizer)
            raise
        # 成功路径：break 退出循环，正常继续解析 usage

        data = response.json()
        content = ""
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
        usage = self._usage_from(data, prompt, content)
        if failures:
            # dataclasses.replace 保留 provider/model/profile_id 等全部字段
            usage = dataclasses.replace(usage, retry_count=usage.retry_count + failures)
        return GenerationResult(raw_text=content, usage=usage)

    def _usage_from(self, data: dict, prompt: str, content: str) -> Usage:
        raw = data.get("usage") or {}
        profile = self._profile
        if self._tokenizer is not None:
            input_tokens = int(raw.get("prompt_tokens") or self._tokenizer.count(prompt))
            output_tokens = int(raw.get("completion_tokens") or self._tokenizer.count(content))
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
            raw = next((v for k, v in self._respond.items() if k in prompt), "{}")
        else:
            raw = self._respond(prompt)
        return GenerationResult(raw_text=raw, usage=self._usage)
