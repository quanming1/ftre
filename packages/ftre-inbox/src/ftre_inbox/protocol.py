"""独立 Inbox 与宿主 Agent 之间的最小输入协议。

这个 DTO 刻意只描述“已经准备交付的一条输入”。它不携带 QueueItem、队列目标或
持久化 revision，因此 Inbox 可以在没有安装完整 ftre Gateway 时被导入和测试。
Gateway 中的 ``AgentService`` 接受同形状的输入对象；两边不需要互相 import。
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
