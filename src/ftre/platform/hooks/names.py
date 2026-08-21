"""F6 语义 Hook 名称。

名称是跨 Plugin 的稳定协议；旧的 ``agent/before_*`` 名称不在这里导出，
迁移完成后由架构测试确保它们不会回流到新代码。
"""

AGENT_CREATED = "agent/created"
AGENT_DISPOSED = "agent/disposed"
AGENT_ERROR = "agent/error"
AGENT_INBOX_INSERTED = "agent/inbox/inserted"
AGENT_INBOX_CLAIMED = "agent/inbox/claimed"
AGENT_INBOX_DISCARDED = "agent/inbox/discarded"
AGENT_PRE_STEP = "agent/pre-step"
AGENT_REQUEST = "agent/request"
AGENT_REQUEST_ERROR = "agent/request-error"
AGENT_SESSION_START = "agent/session-start"
AGENT_STATUS = "agent/status"
AGENT_TURN_STOPPING = "agent/turn-stopping"
AGENT_TURN_STOPPED = "agent/turn-stopped"

SESSION_EVENT = "session/event"
SESSION_FLUSH = "session/flush"
SESSION_CREATED = "session/created"
SESSION_DISPOSED = "session/disposed"

SYSTEM_PROMPT_ASSEMBLE = "system-prompt/assemble"
LLM_STREAM = "llm/stream"

TOOL_PRE_EXECUTE = "tools/pre-execute"
TOOL_EXECUTE = "tools/execute"
TOOL_POST_EXECUTE = "tools/post-execute"
TOOL_RESULT = "tools/result"
TOOL_CHANGE = "tools/change"

PUBLIC_HOOK_NAMES = frozenset(
    {
        AGENT_CREATED,
        AGENT_DISPOSED,
        AGENT_ERROR,
        AGENT_INBOX_INSERTED,
        AGENT_INBOX_CLAIMED,
        AGENT_INBOX_DISCARDED,
        AGENT_PRE_STEP,
        AGENT_REQUEST,
        AGENT_REQUEST_ERROR,
        AGENT_SESSION_START,
        AGENT_STATUS,
        AGENT_TURN_STOPPING,
        AGENT_TURN_STOPPED,
        SESSION_EVENT,
        SESSION_FLUSH,
        SESSION_CREATED,
        SESSION_DISPOSED,
        SYSTEM_PROMPT_ASSEMBLE,
        LLM_STREAM,
        TOOL_PRE_EXECUTE,
        TOOL_EXECUTE,
        TOOL_POST_EXECUTE,
        TOOL_RESULT,
        TOOL_CHANGE,
    }
)

__all__ = ["PUBLIC_HOOK_NAMES"] + [
    name
    for name in tuple(globals())
    if name.isupper() and name != "PUBLIC_HOOK_NAMES"
]
