"""ftre 语义 Hook 的公开契约与 Cordis 适配层。

本包只负责定义稳定名称、调度模式和注册/诊断边界；具体 Agent、Tool、Session
行为仍由各自 Service/Feature 持有，避免重新形成一个全局业务 HookManager。
"""

from .diagnostics import HookDiagnostic, HookListenerSnapshot
from .names import (
    AGENT_AFTER_TURN,
    AGENT_CREATED,
    AGENT_DISPOSED,
    AGENT_ERROR,
    AGENT_INBOX_CLAIMED,
    AGENT_INBOX_DISCARDED,
    AGENT_INBOX_INSERTED,
    AGENT_PRE_STEP,
    AGENT_REQUEST,
    AGENT_REQUEST_ERROR,
    AGENT_SESSION_START,
    AGENT_STATUS,
    AGENT_TURN_STOPPED,
    AGENT_TURN_STOPPING,
    LLM_STREAM,
    PUBLIC_HOOK_NAMES,
    SESSION_CREATED,
    SESSION_DISPOSED,
    SESSION_EVENT,
    SESSION_FLUSH,
    SYSTEM_PROMPT_ASSEMBLE,
    TOOL_CHANGE,
    TOOL_EXECUTE,
    TOOL_POST_EXECUTE,
    TOOL_PRE_EXECUTE,
    TOOL_RESULT,
)
from .runtime import HookReceipt, HookRuntime
from .scope import HookScopeCarrier, context_for_scope
from .spec import HookFailurePolicy, HookMode, HookScope, HookSpec

__all__ = [
    "AGENT_AFTER_TURN",
    "AGENT_CREATED",
    "AGENT_DISPOSED",
    "AGENT_ERROR",
    "AGENT_INBOX_CLAIMED",
    "AGENT_INBOX_DISCARDED",
    "AGENT_INBOX_INSERTED",
    "AGENT_PRE_STEP",
    "AGENT_REQUEST",
    "AGENT_REQUEST_ERROR",
    "AGENT_SESSION_START",
    "AGENT_STATUS",
    "AGENT_TURN_STOPPED",
    "AGENT_TURN_STOPPING",
    "LLM_STREAM",
    "PUBLIC_HOOK_NAMES",
    "SESSION_CREATED",
    "SESSION_DISPOSED",
    "SESSION_EVENT",
    "SESSION_FLUSH",
    "SYSTEM_PROMPT_ASSEMBLE",
    "TOOL_CHANGE",
    "TOOL_EXECUTE",
    "TOOL_POST_EXECUTE",
    "TOOL_PRE_EXECUTE",
    "TOOL_RESULT",
    "HookDiagnostic",
    "HookFailurePolicy",
    "HookListenerSnapshot",
    "HookMode",
    "HookReceipt",
    "HookRuntime",
    "HookScope",
    "HookScopeCarrier",
    "HookSpec",
    "context_for_scope",
]
