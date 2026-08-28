"""Agent 公共 Tool 契约。

这里仅定义跨 Package 可传递的工具声明、调用值模型和权限值模型。
注册、作用域、审批、注入解析与执行由 Host ToolService 持有。
"""

from .contracts import (
    ToolCallRequest,
    ToolContext,
    ToolExecutionResult,
    ToolSchema,
    ToolView,
)
from .definition import Injected, ToolDefinition, ToolParameter, tool
from .permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionRequest,
    PermissionRule,
)

__all__ = [
    "Injected",
    "PermissionBehavior",
    "PermissionContext",
    "PermissionDecision",
    "PermissionRequest",
    "PermissionRule",
    "ToolCallRequest",
    "ToolContext",
    "ToolDefinition",
    "ToolExecutionResult",
    "ToolParameter",
    "ToolSchema",
    "ToolView",
    "tool",
]
