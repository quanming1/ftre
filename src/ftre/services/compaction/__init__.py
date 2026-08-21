"""Public Compaction Service consumed by AgentLoop, ContextGate and Command."""

from .events import CompactEventName
from .service import CompactionService

__all__ = ["CompactEventName", "CompactionService"]
