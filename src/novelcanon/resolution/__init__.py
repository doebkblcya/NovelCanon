"""实体消歧（阶段 08，docs/implementation/08）。"""

from novelcanon.resolution.resolver import (
    EntityResolver,
    ResolutionPlan,
    ResolvedMention,
    is_generic,
    normalize_surface,
)
from novelcanon.resolution.service import ResolutionService, ResolveStats

__all__ = [
    "EntityResolver",
    "ResolutionPlan",
    "ResolvedMention",
    "ResolutionService",
    "ResolveStats",
    "is_generic",
    "normalize_surface",
]
