"""实体消歧 resolver（阶段 08，docs/implementation/08）。

核心职责：把跨章 mention 映射为稳定 canonical entity。

实施链（08 §1–§5，确定性层 v3，P0 验收收紧）：
1. 名称规范化：全半角、空白、可安全归一的标点统一（保留原文写法）；
2. 泛称过滤：识别「男子」「长老」等泛称 → unresolved，不创建错误实体；
3. 确定性规则（v3）：
   - canonical_id 由「book + 稳定首提及锚点（章节位置）」派生，不依赖
     名称 / ordinal / extraction run——不同书同名不共享 ID，改名（身份
     揭示）不改变 ID（08 基本原则）；
   - 同 surface 归组：同章多次提及 → 同一人物（合并）；跨章连续出现或
     任一章节出现多次 → 同一人物（连续性信号）；
   - 跨章孤立出现（每章仅一次且章节序不连续）→ 无法判断 → unresolved
     （「无法判断时进入 unresolved」），不盲目合并同名不同人物；
   - 已验证精确 alias（seed）优先合并（身份揭示走 canonical 层）。
4. 低置信语义判定 / 第二遍复判：阶段 08 只实现确定性层 + 保留扩展点。
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


class EntityResolver:
    """确定性实体消歧（08 §2 优先规则，v3）。

    输入 mention：{mention_id, surface_name, chapter_id, ordinal?,
    char_start?, char_end?}，按披露顺序传入。

    决策规则（全部确定性、可审计）：
    1. 泛称 → unresolved（generic）；
    2. 已验证 alias（seed）→ 合并到 seed canonical（identity reveal）；
    3. 同 (book, surface) 归组：
       - 全部同章（或无章）→ 合并（同章多次提及 = 同一人物，P0：
         不再强制拆开）；
       - 跨章但章节连续（ordinal 无缺口）或任一章节出现 ≥2 次 →
         合并（连续性信号，主角跨章稳定）；
       - 其余（每章孤立出现、章节序不连续）→ 无法判断 → unresolved
         （不盲目合并「不同章节的两个同名人物」，P0）。
    canonical_id = ent_<hash(book, 首提及锚点)>：锚点 = 组内首个 mention
    的 (chapter_id, char_start, char_end)（生产数据必有；测试数据用
    mention_id 兜底）。不依赖名称 → 改名不换 ID；不依赖 ordinal/run。
    """

    RESOLVER_VERSION = "deterministic-v3"

    def __init__(self, book_id: str = "book") -> None:
        self._book_id = book_id
        self._seeded: dict[str, str] = {}  # norm surface -> canonical（跨 run 稳定）
        self._surface_to_canonical: dict[str, str] = {}

    def seed(self, known_aliases: dict[str, str]) -> None:
        """预置已知 alias（surface → canonical），保证跨 run 稳定。

        来自库里已有 alias claim（阶段 07 materialize 已写入）。

        P0：**整份替换**而非增量追加——复用同一 Resolver 处理书 A 后再
        处理书 B 时，书 B 不得命中书 A 的历史 alias（跨书合并）；同书已
        失效 alias 也不得残留。每次 resolve_run 前调用方传入该书当前
        的可信 alias 全集，_seeded 即该书唯一真相。
        """
        self._seeded = {
            normalize_surface(surface): canonical for surface, canonical in known_aliases.items()
        }

    def resolve(
        self,
        mentions: list[dict],
        *,
        book_id: str | None = None,
    ) -> ResolutionPlan:
        """对一批 mention 生成消歧计划（幂等：每次从头计算，不累积状态）。

        可重复调用：相同输入与 book_id 得到相同 canonical 分配。
        """
        book = book_id or self._book_id
        # 每次调用从头计算：seeded alias 固定，其余状态不跨调用残留
        self._surface_to_canonical = dict(self._seeded)
        resolved: list[ResolvedMention] = []
        unresolved: list[ResolvedMention] = []

        generic = [m for m in mentions if is_generic(m["surface_name"])]
        for m in generic:
            unresolved.append(
                ResolvedMention(
                    mention_id=m["mention_id"],
                    surface_name=m["surface_name"],
                    canonical_id=None,
                    reason="generic",
                    resolved=False,
                )
            )

        groups: dict[str, list[dict]] = {}
        for m in mentions:
            if is_generic(m["surface_name"]):
                continue
            groups.setdefault(normalize_surface(m["surface_name"]), []).append(m)

        for norm, group in groups.items():
            seeded = self._surface_to_canonical.get(norm)
            if seeded is not None:
                for m in group:
                    resolved.append(
                        ResolvedMention(
                            mention_id=m["mention_id"],
                            surface_name=m["surface_name"],
                            canonical_id=seeded,
                            reason="seed-alias",
                            resolved=True,
                        )
                    )
                continue
            if self._same_identity(group):
                canonical = self._canonical_for(group, book)
                for m in group:
                    resolved.append(
                        ResolvedMention(
                            mention_id=m["mention_id"],
                            surface_name=m["surface_name"],
                            canonical_id=canonical,
                            reason="exact-surface-match",
                            resolved=True,
                        )
                    )
            else:
                for m in group:
                    unresolved.append(
                        ResolvedMention(
                            mention_id=m["mention_id"],
                            surface_name=m["surface_name"],
                            canonical_id=None,
                            reason="ambiguous-name-no-continuity",
                            resolved=False,
                        )
                    )
        return ResolutionPlan(
            resolved=resolved,
            unresolved=unresolved,
            resolver_version=self.RESOLVER_VERSION,
        )

    # ── 合并判定与 canonical 派生 ───────────────────────────────

    def _same_identity(self, group: list[dict]) -> bool:
        """组内全部 mention 是否判定为同一人物（确定性连续性规则）。

        合并信号（任一满足）：
        - 全部同章（含无章数据）→ 同章多次提及 = 同一人物；
        - 章节序连续（ordinal 无缺口）→ 跨章延续；
        - 任一章节出现 ≥2 次 → 章内复现 = 强连续性。
        否则（跨章孤立出现、章节序不连续）→ 无法判断 → 不合并。
        """
        chapters = {m.get("chapter_id") for m in group if m.get("chapter_id") is not None}
        if len(chapters) <= 1:
            return True
        if any(sum(1 for m in group if m.get("chapter_id") == ch) > 1 for ch in chapters):
            return True  # 某章内出现多次 → 同一人物延续
        ordinals: list[int] = []
        for m in group:
            ordinal = m.get("ordinal")
            if isinstance(ordinal, int):
                ordinals.append(ordinal)
        if len(ordinals) == len(group):
            distinct = sorted(set(ordinals))
            # 章节序连续：max-min+1 == 去重数（无缺口）
            return distinct[-1] - distinct[0] + 1 == len(distinct)
        # 无 ordinal 数据（单元测试）：多章且每章一次 → 保守 unresolved
        return False

    @staticmethod
    def _canonical_for(group: list[dict], book_id: str) -> str:
        """确定性 canonical_id：book + 组内首个 mention 的稳定锚点。

        锚点 = (chapter_id, char_start, char_end)；生产数据必有位置字段，
        测试数据缺失时用 mention_id 兜底（保证不撞、同输入稳定）。
        不包含 surface / ordinal / run → 改名不换 ID、跨 run 稳定。
        """
        first = min(
            group,
            key=lambda m: (
                m.get("ordinal") if isinstance(m.get("ordinal"), int) else 1 << 30,
                m.get("char_start") if isinstance(m.get("char_start"), int) else 1 << 30,
                m.get("mention_id", ""),
            ),
        )
        chapter = first.get("chapter_id") or ""
        anchor: tuple[object, ...]
        if isinstance(first.get("char_start"), int):
            anchor = (
                chapter,
                int(first["char_start"]),
                int(first.get("char_end") or first["char_start"]),
            )
        else:
            anchor = (chapter, first.get("mention_id", ""))
        return "ent_" + stable_config_hash({"book": book_id, "anchor": anchor})[:16]
