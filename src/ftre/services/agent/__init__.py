"""Agent 身份、Driver 合约、生命周期 Hook 和公开 AgentService。"""

from .contracts import AgentDriver, AgentRegistryProtocol, InboundMessage
from .hooks import (
    AGENT_BEFORE_REASONING_SPEC,
    AGENT_BEFORE_TURN_SPEC,
    AGENT_CREATED_SPEC,
    AGENT_DISPOSED_SPEC,
    AGENT_ERROR_SPEC,
    AGENT_REQUEST_ERROR_SPEC,
    AGENT_REQUEST_SPEC,
    AGENT_SESSION_START_SPEC,
    AGENT_STATUS_SPEC,
    AGENT_TURN_STOPPED_SPEC,
    AGENT_TURN_STOPPING_SPEC,
)
from .registry import AgentRegistry
from .service import AgentService

__all__ = [
    "AGENT_BEFORE_REASONING_SPEC",
    "AGENT_BEFORE_TURN_SPEC",
    "AGENT_CREATED_SPEC",
    "AGENT_DISPOSED_SPEC",
    "AGENT_ERROR_SPEC",
    "AGENT_REQUEST_ERROR_SPEC",
    "AGENT_REQUEST_SPEC",
    "AGENT_SESSION_START_SPEC",
    "AGENT_STATUS_SPEC",
    "AGENT_TURN_STOPPED_SPEC",
    "AGENT_TURN_STOPPING_SPEC",
    "AgentDriver",
    "AgentRegistry",
    "AgentRegistryProtocol",
    "AgentService",
    "InboundMessage",
]
