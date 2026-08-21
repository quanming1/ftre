"""ftre 业务路径对 Core Tool Hook 契约的稳定重导出。"""

from ftre_agent_core.hooks import (
    TOOLS_EXECUTE_SPEC,
    TOOLS_POST_EXECUTE_SPEC,
    TOOLS_PRE_EXECUTE_SPEC,
    TOOLS_RESULT_SPEC,
    ToolAllow,
    ToolArguments,
    ToolCallIdentity,
    ToolDeny,
    ToolExecutePayload,
    ToolExecutionResult,
    ToolPostExecutePayload,
    ToolPreExecutePayload,
    ToolResultPayload,
)

__all__ = [
    "TOOLS_EXECUTE_SPEC",
    "TOOLS_POST_EXECUTE_SPEC",
    "TOOLS_PRE_EXECUTE_SPEC",
    "TOOLS_RESULT_SPEC",
    "ToolAllow",
    "ToolArguments",
    "ToolCallIdentity",
    "ToolDeny",
    "ToolExecutePayload",
    "ToolExecutionResult",
    "ToolPostExecutePayload",
    "ToolPreExecutePayload",
    "ToolResultPayload",
]
