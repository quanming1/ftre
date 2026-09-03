"""外部输入进入 Inbox 前的最小消息信封。

这个 DTO 刻意只描述一条待接纳的输入，不携带 QueueItem、队列目标或持久化
revision。Inbox 会在自己的边界内把它转换为 AgentRunRequest；它不是 Agent
Service 的公共输入类型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """交给宿主 Agent 的最小消息信封。"""

    session_id: str
    request_id: str
    channel_id: str
    content: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    source: str = "user"
    metadata: dict[str, Any] | None = None


__all__ = ["InboundMessage"]
