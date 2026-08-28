"""ftre Tool Hook 的 Host 入口。"""

from ftre_agent.hooks import (
    TOOL_AFTER_SPEC,
    TOOL_BEFORE_SPEC,
    ToolAfterPayload,
    ToolAllow,
    ToolArguments,
    ToolBeforePayload,
    ToolCallIdentity,
    ToolDeny,
    ToolExecutionResult,
)

TOOL_BEFORE = TOOL_BEFORE_SPEC.name
TOOL_AFTER = TOOL_AFTER_SPEC.name

__all__ = [
    "TOOL_AFTER",
    "TOOL_AFTER_SPEC",
    "TOOL_BEFORE",
    "TOOL_BEFORE_SPEC",
    "ToolAfterPayload",
    "ToolAllow",
    "ToolArguments",
    "ToolBeforePayload",
    "ToolCallIdentity",
    "ToolDeny",
    "ToolExecutionResult",
]
