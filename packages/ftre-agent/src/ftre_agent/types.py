"""共享类型定义。

独立模块，不依赖 message/event，避免循环 import。
event 和 message 都从这里 import ReplyFinishedReason 等。
"""
from __future__ import annotations

from enum import StrEnum


class ReplyFinishedReason(StrEnum):
    """Turn 结束原因（turn/end.outcome 与 Msg.finished_reason 共用）。"""
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    EXCEED_MAX_ITERS = "exceed_max_iters"
    ERROR = "error"
