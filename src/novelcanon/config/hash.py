"""稳定配置 hash：规范化 JSON + SHA-256（ADR-0003）。"""

from __future__ import annotations

import hashlib
import json


def stable_config_hash(payload: dict[str, object]) -> str:
    """对 dict 生成跨进程稳定的 SHA-256。

    字段排序 + 紧凑分隔符保证相同内容 hash 稳定；payload 值须可 JSON 序列化。
    供 run 幂等、checkpoint 键与 profile 版本比对使用。
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
