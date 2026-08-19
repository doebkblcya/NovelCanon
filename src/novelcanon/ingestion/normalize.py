"""文本规范化（定版方案 §3.1）。

统一执行：UTF-8 解码 → Unicode NFC → 换行统一为 LF。
偏移一律使用 Unicode code point；区间一律为半开区间 [start, end)。
规范化函数必须纯粹且幂等（normalize(normalize(x)) == normalize(x)）。
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedText:
    """规范化文本及其内容 hash。"""

    text: str
    content_hash: str

    def __len__(self) -> int:
        return len(self.text)


def decode_bytes(raw: bytes, *, encoding: str = "utf-8") -> str:
    """字节解码；失败时抛 ValueError，由调用方决定回退策略。"""
    return raw.decode(encoding)


def normalize_text(text: str) -> str:
    """规范化：NFC + LF 统一。幂等：对规范化结果再执行结果不变。"""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def normalize_bytes(raw: bytes) -> NormalizedText:
    """从字节流得到规范化文本（UTF-8 解码 + 规范化 + hash）。"""
    text = normalize_text(decode_bytes(raw))
    return NormalizedText(text=text, content_hash=sha256(text))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def span_hash(text: str) -> str:
    """证据 span 的内容 hash（定版方案 §6：直接证据须能在规范化原文上复现）。"""
    return sha256(text)


def slice_by_char_range(text: str, start: int, end: int) -> str:
    """半开区间 [start, end) 的 code point 切片；越界抛 IndexError。"""
    if start < 0 or end < start or end > len(text):
        raise IndexError(f"char range [{start}, {end}) 超出文本长度 {len(text)}")
    return text[start:end]
