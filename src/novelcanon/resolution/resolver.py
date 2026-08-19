"""实体消歧 resolver（阶段 08，docs/implementation/08）。

核心职责：把跨章 mention 映射为稳定 canonical entity。

实施链（08 §1–§4）：
1. 名称规范化：全半角、空白、可安全归一的标点统一（保留原文写法）；
2. 泛称过滤：识别「男子」「长老」等泛称 → unresolved，不创建错误实体；
3. 确定性规则：规范化 surface 完全相同的 mention 归入同一 canonical
   （canonical 通过 alias claim 复用，跨 run 稳定）；
4. 候选生成/低置信语义判定：阶段 08 只实现确定性层 + 保留扩展点
   （低置信调用模型属后续增强，本阶段不引入模型依赖）。

canonical_id 稳定生成：新实体用 UUID；已存在的实体经 alias 命中复用
（canonical_id 不依赖名称/ordinal/run，08 §基本原则）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from novelcanon.schemas.ids import new_uuid_id

# 名称规范化（08 §1）：全半角、空白、标点
_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ　！＂＃＄％＆＇（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～",
    "0123456789abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~",
)
_WHITESPACE = re.compile(r"\s+")
# 可安全归一的标点（中文书名号/引号/间隔号 → 统一形态）
_PUNCT_MAP = str.maketrans(
    {"“": '"', "”": '"', "‘": "'", "’": "'", "《": "<", "》": ">", "·": "·"}
)

# 泛称黑名单（08 §1）：称谓/描述性指称，不作为独立实体
GENERIC_SURFACES = {
    "男子",
    "中年男子",
    "少年",
    "少女",
    "青年",
    "老人",
    "长老",
    "掌门",
    "弟子",
    "母亲",
    "父亲",
    "姐姐",
    "哥哥",
    "妹妹",
    "弟弟",
    "众人",
    "围观者",
    "镇上人",
    "同门弟子",
}


@dataclass(frozen=True)
class ResolvedMention:
    """一条 mention 的消歧结果。"""

    mention_id: str
    surface_name: str
    canonical_id: str | None  # None = unresolved
    reason: str
    resolved: bool


@dataclass(frozen=True)
class ResolutionPlan:
    """一次消歧的完整结果（可重放/审计）。"""

    resolved: list[ResolvedMention]
    unresolved: list[ResolvedMention]
    resolver_version: str


def normalize_surface(surface: str) -> str:
    """名称规范化：全半角/空白/标点统一（保留原文写法，仅归一可安全归一的形态）。"""
    text = surface.translate(_FULLWIDTH).translate(_PUNCT_MAP)
    return _WHITESPACE.sub("", text).strip()


def is_generic(surface: str) -> bool:
    """泛称判定（08 §1）：避免直接创建错误实体。"""
    return normalize_surface(surface) in GENERIC_SURFACES


class EntityResolver:
    """确定性实体消歧（08 §2 优先规则）。

    同名合并：规范化 surface 相同 → 同一 canonical。canonical 映射经
    alias 复用（第一次遇到某 surface 时分配 UUID，后续命中复用），
    跨 run 相同输入得到相同 canonical_id。
    """

    RESOLVER_VERSION = "deterministic-v1"

    def __init__(self) -> None:
        self._surface_to_canonical: dict[str, str] = {}

    def seed(self, known_aliases: dict[str, str]) -> None:
        """预置已知 alias（surface → canonical），保证跨 run 稳定。

        来自库里已有 alias claim（阶段 07 materialize 已写入）。
        """
        for surface, canonical in known_aliases.items():
            norm = normalize_surface(surface)
            self._surface_to_canonical.setdefault(norm, canonical)

    def resolve(self, mentions: list[dict]) -> ResolutionPlan:
        """对一批 mention 生成消歧计划。

        mentions: [{mention_id, surface_name, ...}]，按披露顺序传入
        （先出现的 surface 分配 canonical，后续同名复用）。
        """
        resolved: list[ResolvedMention] = []
        unresolved: list[ResolvedMention] = []
        for m in mentions:
            mid = m["mention_id"]
            surface = m["surface_name"]
            norm = normalize_surface(surface)
            if is_generic(surface):
                unresolved.append(
                    ResolvedMention(
                        mention_id=mid,
                        surface_name=surface,
                        canonical_id=None,
                        reason="generic",
                        resolved=False,
                    )
                )
                continue
            canonical = self._surface_to_canonical.get(norm)
            if canonical is None:
                canonical = new_uuid_id("ent")
                self._surface_to_canonical[norm] = canonical
            resolved.append(
                ResolvedMention(
                    mention_id=mid,
                    surface_name=surface,
                    canonical_id=canonical,
                    reason="exact-surface-match",
                    resolved=True,
                )
            )
        return ResolutionPlan(
            resolved=resolved,
            unresolved=unresolved,
            resolver_version=self.RESOLVER_VERSION,
        )
