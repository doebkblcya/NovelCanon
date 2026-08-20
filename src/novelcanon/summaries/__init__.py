"""分层摘要（阶段 10，docs/implementation/10 §6/§7）。

章节结构化记忆 → 卷摘要 → 全书摘要；卷分组（原书卷标题 / 每 50 章
默认）与可失效重建（输入 claim 变化 → 依赖图标记 stale）。
"""

from novelcanon.summaries.reducer import (
    ChapterMemory,
    DeterministicSummarizer,
    HierarchicalReducer,
    LLMSummarizer,
    Summarizer,
    SummaryResult,
)
from novelcanon.summaries.volumes import (
    DEFAULT_CHAPTERS_PER_VOLUME,
    GROUPING_VERSION,
    GroupingResult,
    VolumeGroup,
    VolumeGrouper,
    list_active_volumes,
)

__all__ = [
    "ChapterMemory",
    "DEFAULT_CHAPTERS_PER_VOLUME",
    "DeterministicSummarizer",
    "GROUPING_VERSION",
    "GroupingResult",
    "HierarchicalReducer",
    "LLMSummarizer",
    "Summarizer",
    "SummaryResult",
    "VolumeGroup",
    "VolumeGrouper",
    "list_active_volumes",
]
