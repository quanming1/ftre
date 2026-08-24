"""Agent 身份、Driver 合约、生命周期 Hook 和公开 AgentService。"""

from .contracts import AgentDriver, AgentRegistryProtocol, InboundMessage
from .hooks import (
    AGENT_AFTER_RUN_SPEC,
    AGENT_BEFORE_REASONING_SPEC,
    AGENT_BEFORE_RUN_SPEC,
    AGENT_RUN_ERROR_SPEC,
    AGENT_STOP_DECISION_SPEC,
)
from .registry import AgentRegistry
from .service import AgentService

__all__ = [
    "AGENT_AFTER_RUN_SPEC",
    "AGENT_BEFORE_REASONING_SPEC",
    "AGENT_BEFORE_RUN_SPEC",
    "AGENT_RUN_ERROR_SPEC",
    "AGENT_STOP_DECISION_SPEC",
    "AgentDriver",
    "AgentRegistry",
    "AgentRegistryProtocol",
    "AgentService",
    "InboundMessage",
]
