"""逐章 Map 抽取（阶段 06 实现）与固定 Draft 落库（阶段 05）。"""

from novelcanon.extraction.map_pipeline import MapClient, build_map_process_fn
from novelcanon.extraction.materialize import MaterializeStats, materialize_draft
from novelcanon.extraction.staging import MapStaging, draft_id

__all__ = [
    "MapClient",
    "MapStaging",
    "MaterializeStats",
    "build_map_process_fn",
    "draft_id",
    "materialize_draft",
]
