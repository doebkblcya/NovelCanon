"""调用治理（阶段 04，docs/implementation/04 §4）。

- 全局令牌桶：控制请求数 / Token / 并发；
- 带抖动的指数退避重试；
- Schema 或输入无效属于不可重试错误（NonRetryableError）；
- 记录被丢弃响应，防止重试成本不可见。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass


class RetryableError(Exception):
    """可重试错误（超时、限流、瞬时网络错误等）。"""


class NonRetryableError(Exception):
    """不可重试错误（Schema 校验失败、输入无效等），直接标记失败。"""


class TokenBucket:
    """全局令牌桶（asyncio 单消费者安全）。"""

    def __init__(self, capacity: int = 10, refill_per_second: float = 10.0) -> None:
        self._capacity = float(capacity)
        self._refill_rate = refill_per_second
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def _refill_tokens(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._refill_rate)
        self._updated = now

    async def acquire(self, n: int = 1) -> None:
        """取 n 个令牌；不足时等待（背压而非拒绝）。"""
        async with self._lock:
            while True:
                await self._refill_tokens()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                await asyncio.sleep(0.01)


@dataclass(frozen=True)
class RetryPolicy:
    """带抖动的指数退避。"""

    max_attempts: int = 3
    base_delay: float = 0.1
    max_delay: float = 2.0
    jitter: float = 0.1

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次失败后的等待时长（attempt 从 0 起）。"""
        delay = min(self.base_delay * (2**attempt), self.max_delay)
        return delay * (1 + random.uniform(-self.jitter, self.jitter))
