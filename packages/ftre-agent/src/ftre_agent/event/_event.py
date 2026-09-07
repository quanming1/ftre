"""Runtime 跨边界输入协议事件。

日志事件（session.events 的 13 种，PRD-F41 附录 A）之外，Runtime 还有两类
输入侧协议对象，它们不进入 SessionLog：
- ``UserConfirmResultEvent``：HITL 恢复输入（agent.run 的参数，不上 wire）；
- ``HintBlockEvent``：工具执行结果中的 hint 载体（ToolExecutionResult.event，
  由 ActingExecutor 转译为 hint/message 日志事件）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..message import DataBlock, TextBlock


def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class EventBase(BaseModel):
    """事件基类（pydantic，model_dump 扁平序列化）。"""
    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(default_factory=_gen_id)
    created_at: str = Field(default_factory=_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)
    message_id: str | None = None


class HintBlockEvent(EventBase):
    """工具产出的提示块载体：ActingExecutor 将其转译为 hint/message 事件。"""
    type: Literal["HINT_BLOCK"] = "HINT_BLOCK"
    reply_id: str = ""
    block_id: str
    source: str | None = None
    hint: str | list[TextBlock | DataBlock]


class UserConfirmResultEvent(EventBase):
    """用户对某个待确认工具调用的决定（输入事件，驱动 run() 恢复）。

    - approved=True  → 该工具调用从 ASKING 转 ALLOWED，恢复后执行
    - approved=False → 产生 DENIED 工具结果，不执行

    ``reply_id`` 与 ``tool_call_id`` 必须与挂起时一致，否则视为非法输入被拒绝。
    """
    type: Literal["USER_CONFIRM_RESULT"] = "USER_CONFIRM_RESULT"
    reply_id: str
    tool_call_id: str
    approved: bool


__all__ = [
    "EventBase",
    "HintBlockEvent",
    "UserConfirmResultEvent",
]
