"""Tokenizer 协议（ADR-0006）。

raw chunk 按 embedding tokenizer 切分；tiktoken 只是可选适配器，
FakeTokenizer 为确定性测试/无外部依赖基线。generation/embedding profile
分别指定 tokenizer_id。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Tokenizer(Protocol):
    """统一 tokenizer 接口。"""

    tokenizer_id: str

    def encode(self, text: str) -> list[int]: ...
    def decode(self, tokens: Sequence[int]) -> str: ...
    def count(self, text: str) -> int: ...
    def token_char_offsets(self, text: str) -> list[int]:
        """每个 token 的字符起始偏移（长度 = token 数 + 1，末项 = len(text)）。

        chunk 切分需要 token 区间 ↔ char 区间 的双向映射（§3.3）。
        """
        ...


class FakeTokenizer:
    """确定性伪 tokenizer：每个 code point 记 1 个 token。

    用于无外部依赖的契约测试与 Pilot 基线；token 与 char 1:1 映射，
    decode 语义不完整（仅切分/计量用途）。
    """

    tokenizer_id = "fake-v1"

    def encode(self, text: str) -> list[int]:
        return list(range(len(text)))

    def decode(self, tokens: Sequence[int]) -> str:
        raise NotImplementedError("FakeTokenizer 仅用于切分/计量，不支持精确解码")

    def count(self, text: str) -> int:
        return len(text)

    def token_char_offsets(self, text: str) -> list[int]:
        return list(range(len(text) + 1))


class TiktokenAdapter:
    """tiktoken 适配器（可选依赖 tokenizer extra）。

    token→char 偏移通过逐 token 解码累计长度估算（BPE 边界近似）。
    """

    def __init__(self, encoding: str = "o200k_base") -> None:
        import tiktoken  # type: ignore[import-untyped]

        self._enc = tiktoken.get_encoding(encoding)
        self.tokenizer_id = f"tiktoken-{encoding}"

    def encode(self, text: str) -> list[int]:
        return self._enc.encode(text)

    def decode(self, tokens: Sequence[int]) -> str:
        return self._enc.decode(list(tokens))

    def count(self, text: str) -> int:
        return len(self._enc.encode(text))

    def token_char_offsets(self, text: str) -> list[int]:
        ids = self._enc.encode(text)
        offsets = [0]
        for tok in ids:
            piece = self._enc.decode([tok])
            offsets.append(offsets[-1] + len(piece))
        if offsets and offsets[-1] < len(text):
            offsets.append(len(text))
        return offsets
