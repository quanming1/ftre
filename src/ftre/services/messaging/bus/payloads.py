"""Gateway 自有 Bus Payload 模型。

核心 Agent 事件由 ``ftre-agent`` 定义；本模块只约束 Gateway
自己拥有的 session/global 协议，避免业务代码继续拼接裸字典。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(extra="forbid", frozen=True)


class CommandMessagePayload(BaseModel):
    """不运行 Agent 的 slash command 给客户端展示的文本。"""

    model_config = _STRICT

    content: str
    level: Literal["info", "warning", "error"] = "info"
