"""事件链接（阶段 09，docs/implementation/09）。"""

from novelcanon.events.linker import (
    EventInfo,
    EventLinker,
    LinkCandidate,
)
from novelcanon.events.service import EventLinkService, LinkStats

__all__ = [
    "EventInfo",
    "EventLinker",
    "EventLinkService",
    "LinkCandidate",
    "LinkStats",
]
