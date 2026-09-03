"""ReAct Runtime 持久上下文。"""

from __future__ import annotations

from ftre_agent.message import Msg
from ftre_agent.tool.permission import PermissionContext
from pydantic import BaseModel, Field


class AgentState(BaseModel):
    """可注入新 Runtime Agent 的消息与权限快照。"""

    context: list[Msg] = Field(default_factory=list)
    permission_context: PermissionContext = Field(default_factory=PermissionContext)


__all__ = ["AgentState"]
