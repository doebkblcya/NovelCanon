"""实体消歧 resolver（阶段 08，docs/implementation/08）。

核心职责：把跨章 mention 映射为稳定 canonical entity。

实施链（08 §1–§4）：
1. 名称规范化：全半角、空白、可安全归一的标点统一（保留原文写法）；
2. 泛称过滤：识别「男子」「长老」等泛称 → unresolved，不创建错误实体；
3. 确定性规则：规范化 surface 完全相同的 mention 归入同一 canonical
   （canonical 经 alias 复用，跨 run 稳定）；
4. 同名冲突保护（P0 修复）：同一章内出现两个同 surface 的不同 mention
   （同章同名）→ 不自动合并（极可能为不同人物），保持独立 canonical
   或 unresolved，避免「同名不同人物」误合并；
5. 候选生成/低置信语义判定：阶段 08 只实现确定性层 + 保留扩展点。

canonical_id 确定性（P0 修复）：由「规范化 surface 的稳定 hash」派生
（ent_<hash16>），不依赖 ordinal / extraction run / 随机 UUID——相同输入
与配置下干净重建得到相同 canonical_id（08 验证项）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from novelcanon.config.hash import stable_config_hash

# 名称规范化（08 §1）：全半角、空白、标点
_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ　！＂＃＄％＆＇（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～",  # noqa: E501
    "0123456789abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~",
)
_WHITESPACE = re.compile(r"\s+")

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
    text = surface.translate(_FULLWIDTH)
    return _WHITESPACE.sub("", text).strip()


def is_generic(surface: str) -> bool:
    """泛称判定（08 §1）：避免直接创建错误实体。"""
    return normalize_surface(surface) in GENERIC_SURFACES


def deterministic_canonical_id(surface: str) -> str:
    """确定性 canonical_id：hash(normalized surface)，不依赖 run/ordinal。

    相同输入与配置下稳定（P0 修复：不再用随机 UUID）。
    """
    return "ent_" + stable_config_hash(
        {"surface": normalize_surface(surface)}
    )[:16]


class EntityResolver:
    """确定性实体消歧（08 §2 优先规则）。

    同名合并：规范化 surface 相同 → 同一 canonical；canonical_id 由
    首个 surface 确定性派生（跨 run 稳定）。同章同名冲突保护：同一章
    内两个同 surface 的不同 mention 不自动合并。
    """

    RESOLVER_VERSION = "deterministic-v2"

    def __init__(self) -> None:
        self._surface_to_canonical: dict[str, str] = {}
        # surface → canonical 由哪个 mention 首次建立（用于同章冲突检测）
        self._surface_mentions: dict[str, list[dict]] = {}

    def seed(self, known_aliases: dict[str, str]) -> None:
        """预置已知 alias（surface → canonical），保证跨 run 稳定。

        来自库里已有 alias claim（阶段 07 materialize 已写入）。
        """
        for surface, canonical in known_aliases.items():
            norm = normalize_surface(surface)
            self._surface_to_canonical.setdefault(norm, canonical)

    def resolve(self, mentions: list[dict]) -> ResolutionPlan:
        """对一批 mention 生成消歧计划。

        mentions: [{mention_id, surface_name, chapter_id, ...}]，按披露
        顺序传入（先出现的 surface 分配 canonical，后续同名复用）。
        可重复调用：每次从头计算，不累积状态（幂等，P0）。
        """
        self._surface_mentions = {}
        resolved: list[ResolvedMention] = []
        unresolved: list[ResolvedMention] = []
        # 第一遍：登记每个 surface 出现的章节（同章同名冲突检测）
        for m in mentions:
            norm = normalize_surface(m["surface_name"])
            if is_generic(m["surface_name"]):
                continue
            self._surface_mentions.setdefault(norm, []).append(
                {"mention_id": m["mention_id"], "chapter_id": m.get("chapter_id")}
            )
        # 冲突 surface：同一非空章节内出现两个以上同 surface mention
        conflict_surfaces = {
            norm
            for norm, ms in self._surface_mentions.items()
            if any(
                ch is not None
                and sum(1 for x in ms if x["chapter_id"] == ch) > 1
                for ch in {x["chapter_id"] for x in ms}
            )
        }

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
            if norm in conflict_surfaces:
                # 同章同名：不自动合并（P0：避免同名不同人物误合并），
                # 每个 mention 独立 canonical
                conflict_canonical = deterministic_canonical_id(surface + ":" + mid)
                resolved.append(
                    ResolvedMention(
                        mention_id=mid,
                        surface_name=surface,
                        canonical_id=conflict_canonical,
                        reason="same-chapter-name-conflict",
                        resolved=True,
                    )
                )
                continue
            canonical: str | None = self._surface_to_canonical.get(norm)
            if canonical is None:
                canonical = deterministic_canonical_id(surface)
                self._surface_to_canonical[norm] = canonical
            assert canonical is not None
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
