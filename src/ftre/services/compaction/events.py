"""Stable SessionEvent names emitted by the Compaction Service."""

from enum import StrEnum


class CompactEventName(StrEnum):
    """CustomEvent.name values projected by SessionProjection."""

    START = "context_compact_start"
    DONE = "context_compact_done"
    FAILED = "context_compact_failed"


__all__ = ["CompactEventName"]
