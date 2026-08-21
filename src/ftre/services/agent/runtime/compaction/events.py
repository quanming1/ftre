"""Context compact 的 session 级 CustomEvent 协议名。"""

from enum import StrEnum


class CompactEventName(StrEnum):
    """CustomEvent.name 的稳定协议值；终态统一叫 DONE，不再引入 END。"""

    START = "context_compact_start"
    DONE = "context_compact_done"
    FAILED = "context_compact_failed"
