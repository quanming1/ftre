"""压缩维护事件的稳定名称。"""


class CompactEventName:
    """Compaction callback names consumed by the injected Host sink."""

    START = "context_compact_start"
    DONE = "context_compact_done"
    FAILED = "context_compact_failed"


__all__ = ["CompactEventName"]
