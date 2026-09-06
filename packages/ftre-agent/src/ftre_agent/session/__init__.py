"""Session 事件日志包——ftre 协议心脏（纯逻辑，无 IO、无 Host 依赖）。

- events.py：13 种事件的契约模型（F41 §4.2 唯一实现）
- log.py：SessionLog 内存提交点（append/load/幂等索引/订阅通知）
- derive.py：derive_messages / derive_context_messages 读侧 fold
"""
from .derive import derive_context_messages, derive_messages
from .events import (
    ALL_EVENT_TYPES,
    IGNORABLE_EVENT_TYPES,
    SURFACE_EVENT_TYPES,
)
from .log import SessionLog

__all__ = [
    "ALL_EVENT_TYPES",
    "IGNORABLE_EVENT_TYPES",
    "SURFACE_EVENT_TYPES",
    "SessionLog",
    "derive_context_messages",
    "derive_messages",
]
