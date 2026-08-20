"""规则预扫描（阶段 11 压缩实验 §1，docs/implementation/11）。

扫描专名、数字、时间、高频意象和重复短语，生成逐字保留词典——
不调用生成模型（11 §1「规则预扫描…不调用生成模型」）。

KeepDict.version 记录 prescan 规则版本 + 输入 hash（compression version
可追踪）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from novelcanon.config.hash import stable_config_hash

PRESCAN_VERSION = "prescan-v1"

# 中文数字（时间/年龄/数量常用）
_CN_NUM_RE = re.compile(r"[0-9０-９零〇一二两三四五六七八九十百千万亿]+")
# 时间表达：年月日时分秒 / 朝代年份
_TIME_RE = re.compile(
    r"(\d{1,4}年|\d{1,2}月|\d{1,2}日|\d{1,2}时|\d{1,2}刻|"
    r"[零一二三四五六七八九十百]+年|[上下]午|清晨|黄昏|深夜|拂晓)"
)


@dataclass(frozen=True)
class KeepDict:
    """逐字保留词典：实体/数字/时间/高频词（压缩必须保留）。"""

    entities: frozenset[str] = frozenset()
    numbers: frozenset[str] = frozenset()
    time_exprs: frozenset[str] = frozenset()
    frequent: frozenset[str] = frozenset()
    version: str = ""

    def all_terms(self) -> frozenset[str]:
        return self.entities | self.numbers | self.time_exprs | self.frequent


class Prescanner:
    """规则预扫描：从章节文本提取必须逐字保留的词项。"""

    def __init__(
        self,
        *,
        top_frequent: int = 100,
        min_freq: int = 3,
        ngram: int = 2,
        version: str = PRESCAN_VERSION,
    ) -> None:
        self._top_frequent = top_frequent
        self._min_freq = min_freq
        self._ngram = ngram
        self._version = version

    # ── 对外 ────────────────────────────────────────────────────

    def scan(
        self,
        texts: list[str],
        *,
        known_surfaces: list[str] | None = None,
    ) -> KeepDict:
        """预扫描：实体（已知表面名）+ 数字 + 时间 + 高频词。"""
        entities = self._scan_entities(texts, known_surfaces or [])
        numbers = self._scan_numbers(texts)
        times = self._scan_times(texts)
        frequent = self._scan_frequent(texts)
        input_hash = stable_config_hash(
            {
                "texts": [t[:200] for t in texts],  # 输入指纹（截断防膨胀）
                "entities": sorted(entities),
            }
        )
        return KeepDict(
            entities=frozenset(entities),
            numbers=frozenset(numbers),
            time_exprs=frozenset(times),
            frequent=frozenset(frequent),
            version=stable_config_hash({"policy": self._version, "input": input_hash}),
        )

    # ── 各类扫描 ────────────────────────────────────────────────

    @staticmethod
    def _scan_entities(texts: list[str], known_surfaces: list[str]) -> list[str]:
        """专名：已知表面名（实体库 alias）+ 高频非停用词。"""
        out: set[str] = set()
        for s in known_surfaces:
            s = s.strip()
            if len(s) >= 2:
                out.add(s)
        # jieba 高频词补充（长度 2–6，非纯数字）
        from collections import Counter

        import jieba  # type: ignore[import-untyped]

        freq: Counter[str] = Counter()
        for text in texts:
            for w in jieba.cut(text):
                w = w.strip()
                if 2 <= len(w) <= 6 and not _CN_NUM_RE.fullmatch(w):
                    freq[w] += 1
        for w, n in freq.most_common(80):
            if n >= 2:
                out.add(w)
        return sorted(out)

    @staticmethod
    def _scan_numbers(texts: list[str]) -> list[str]:
        out: set[str] = set()
        for text in texts:
            for m in _CN_NUM_RE.findall(text):
                if len(m) <= 12:
                    out.add(m)
        return sorted(out)

    @staticmethod
    def _scan_times(texts: list[str]) -> list[str]:
        out: set[str] = set()
        for text in texts:
            for m in _TIME_RE.findall(text):
                out.add(m)
        return sorted(out)

    def _scan_frequent(self, texts: list[str]) -> list[str]:
        """重复短语：n-gram 词频（高频意象/固定搭配）。"""
        from collections import Counter

        freq: Counter[str] = Counter()
        for text in texts:
            chars = list(text)
            for i in range(len(chars) - self._ngram + 1):
                gram = "".join(chars[i : i + self._ngram])
                if not gram.strip() or _CN_NUM_RE.fullmatch(gram):
                    continue
                if not re.search(r"[\u4e00-\u9fff]", gram):
                    continue  # 不含汉字：标点/空白噪声，不算保留词
                freq[gram] += 1
        return [gram for gram, n in freq.most_common(self._top_frequent) if n >= self._min_freq]
