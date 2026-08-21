"""Gateway 自有 Bus Payload 模型。

核心 Agent 事件仍由 ``ftre-agent-core`` 定义；本模块只约束 Gateway
自己拥有的 session/global 协议，避免业务代码继续拼接裸字典。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

_STRICT = ConfigDict(extra="forbid", frozen=True)


class MailboxPhase(str, Enum):
    """SessionLane 对外公布的派生运行状态。

    它不是第二份状态机：值由 Lane 当前 operation 与 MailboxState 共同推导，
    让客户端只需要订阅一个完整快照。
    """

    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPACTING = "compacting"
    BLOCKED = "blocked"


class MailboxItemPayload(BaseModel):
    """等待队列中一条请求的可展示只读视图。"""

    model_config = _STRICT

    request_id: str
    sequence: NonNegativeInt
    content: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    # 保留给当前 Desktop 的展示类型；后端所有 mailbox 请求都视为 user。
    source: str = "user"


class SessionMailboxSnapshotPayload(BaseModel):
    """session_event:mailbox_snapshot 的唯一会话队列投影。"""

    model_config = _STRICT

    session_id: str
    revision: NonNegativeInt
    phase: MailboxPhase
    pending: list[MailboxItemPayload] = Field(default_factory=list)
    capacity: NonNegativeInt
    accepting_messages: bool
    can_cancel_active: bool
    blocked_reason: str | None = None


class CommandMessagePayload(BaseModel):
    """不运行 Agent 的 slash command 给客户端展示的文本。"""

    model_config = _STRICT

    content: str
    level: Literal["info", "warning", "error"] = "info"
