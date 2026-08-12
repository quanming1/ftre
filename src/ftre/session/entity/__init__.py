"""Entity 层：Session 持久化数据模型（映射 state.json 结构）。

本层只有 Pydantic 数据结构与版本校验，不含任何存储/业务逻辑。
"""
from .state import (
    AgentStateFile,
    CURRENT_SCHEMA_VERSION,
    MailboxState,
    QueueItem,
    SessionState,
    UnsupportedAgentStateVersion,
    parse_agent_state,
    parse_agent_state_json,
)

__all__ = [
    "AgentStateFile",
    "CURRENT_SCHEMA_VERSION",
    "MailboxState",
    "QueueItem",
    "SessionState",
    "UnsupportedAgentStateVersion",
    "parse_agent_state",
    "parse_agent_state_json",
]
