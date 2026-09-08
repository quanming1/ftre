"""Gateway 自有 Bus Payload 模型。

会话事件一律经 ``downstream_frame``（wire 帧定义见 messaging/wire.py）；
本模块不再承载 Host 业务 payload。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(extra="forbid", frozen=True)


class CommandMessagePayload(BaseModel):
    """不运行 Agent 的 slash command 给客户端展示的文本（maintenance 帧 value）。"""

    model_config = _STRICT

    content: str
    level: str = "info"
