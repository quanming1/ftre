"""共享类型定义。

独立模块，不依赖 message/event，避免循环 import。
event 和 message 都从这里 import ReplyFinishedReason 等。
"""
from __future__ import annotations

from enum import StrEnum


class ReplyFinishedReason(StrEnum):
    """ReplyEndEvent 的结束原因（对齐 AgentScope types.ReplyFinishedReason）。"""
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    EXCEED_MAX_ITERS = "exceed_max_iters"
    ERROR = "error"
