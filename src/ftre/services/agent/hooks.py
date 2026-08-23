"""Agent 运行时 Hook 契约。

这里仅描述 Agent active Turn 的语义：请求解析、请求错误、Step 边界、Turn 收尾和
生命周期。pending、队列目标以及 claim 观察都属于 ``ftre-inbox`` Package，绝不在
本模块不定义 Inbox 的 pending 类型。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ftre_agent_core.hooks import (
    AGENT_BEFORE_REASONING_SPEC,
    AGENT_TURN_STOPPING_SPEC,
    BeforeReasoningPayload,
    BeforeReasoningResult,
    ContinueTurn,
    StopTurn,
    TurnStoppingPayload,
)

from ftre.platform.hooks import (
    AGENT_AFTER_TURN,
    AGENT_BEFORE_TURN,
    AGENT_CREATED,
    AGENT_DISPOSED,
    AGENT_ERROR,
    AGENT_REQUEST,
    AGENT_REQUEST_ERROR,
    AGENT_SESSION_START,
    AGENT_STATUS,
    AGENT_TURN_STOPPED,
    HookFailurePolicy,
    HookMode,
    HookScope,
    HookSpec,
)
from ftre.services.agent.config import AgentConfig


@dataclass(frozen=True, slots=True)
class AgentSubject:
    """Agent scope carrier；identity 必须跨生命周期唯一。"""

    agent_id: str
    identity: object


@dataclass(frozen=True, slots=True)
class BeforeTurnPayload:
    """一次 InboundMessage 进入 Agent 前的 Turn 级准入输入。"""

    agent: AgentSubject
    session_id: str
    turn_id: str
    cancellation: asyncio.Event
    channel_id: str = ""
    config: AgentConfig | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AllowTurn:
    """默认允许本次 InboundMessage 创建 active Turn。"""

    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RejectTurn:
    """阻止本次 InboundMessage 进入 active Turn；pending 仍由 Inbox 持有。"""

    reason: str


@dataclass(frozen=True, slots=True)
class AfterTurnPayload:
    """Turn 完成后的可等待维护边界。"""

    agent: AgentSubject
    session_id: str
    turn_id: str
    request_id: str
    status: str
    cancellation: asyncio.Event
    channel_id: str = ""
    config: AgentConfig | None = None
    set_maintenance: Callable[[bool, str], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class AgentRequestPayload:
    """一次已交付 Agent request 的 Hook 输入。"""

    agent: AgentSubject
    session_id: str
    turn_id: str
    config: AgentConfig
    cancellation: asyncio.Event


@dataclass(frozen=True, slots=True)
class RequestErrorPayload:
    """LLM/Tool 请求失败时传给错误 Hook 的结构化上下文。"""

    agent: AgentSubject
    session_id: str
    turn_id: str
    error_code: str
    message: str
    attempt: int
    cancellation: asyncio.Event
    channel_id: str = ""
    config: AgentConfig | None = None


@dataclass(frozen=True, slots=True)
class RetryRequest:
    """错误 Hook 请求重新执行当前请求，并携带最大重试次数。"""

    reason: str
    progress_token: str
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if not self.progress_token.strip():
            raise ValueError("RetryRequest.progress_token must be non-empty")
        if self.max_attempts < 1:
            raise ValueError("RetryRequest.max_attempts must be positive")


@dataclass(frozen=True, slots=True)
class TurnStoppedPayload:
    """已完成停止决策后的只读通知。"""

    agent: AgentSubject
    session_id: str
    turn_id: str
    status: str
    request_id: str
    cancellation: asyncio.Event


@dataclass(frozen=True, slots=True)
class AgentLifecyclePayload:
    """Agent 生命周期观察视图；只包含身份和状态坐标。"""

    agent: AgentSubject
    state: str
    session_id: str = ""
    turn_id: str = ""
    error_code: str = ""
    message: str = ""


async def _allow_turn(payload: BeforeTurnPayload) -> AllowTurn:
    return AllowTurn(payload.context)


async def _keep_request(payload: AgentRequestPayload) -> AgentConfig:
    return payload.config


async def _stop_on_error(payload: RequestErrorPayload) -> None:
    return None


async def _continue_after_turn(payload: AfterTurnPayload) -> None:
    return None


def _observe_nothing(_payload) -> None:
    return None


def _observation_spec(name: str, payload_type: type) -> HookSpec:
    return HookSpec(
        name,
        "agent",
        HookMode.EMIT,
        failure_policy=HookFailurePolicy.OBSERVE,
        payload_type=payload_type,
        result_type=type(None),
        default=_observe_nothing,
        scope=HookScope.AGENT,
    )


AGENT_BEFORE_TURN_SPEC = HookSpec(
    AGENT_BEFORE_TURN,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=BeforeTurnPayload,
    result_type=(AllowTurn, RejectTurn),
    default=_allow_turn,
    scope=HookScope.AGENT,
)

AGENT_AFTER_TURN_SPEC = HookSpec(
    AGENT_AFTER_TURN,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=AfterTurnPayload,
    result_type=type(None),
    default=_continue_after_turn,
    scope=HookScope.AGENT,
)

AGENT_REQUEST_SPEC = HookSpec(
    AGENT_REQUEST,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=AgentRequestPayload,
    result_type=AgentConfig,
    default=_keep_request,
    scope=HookScope.AGENT,
)

AGENT_REQUEST_ERROR_SPEC = HookSpec(
    AGENT_REQUEST_ERROR,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=RequestErrorPayload,
    result_type=(RetryRequest, type(None)),
    default=_stop_on_error,
    scope=HookScope.AGENT,
)

AGENT_TURN_STOPPED_SPEC = _observation_spec(AGENT_TURN_STOPPED, TurnStoppedPayload)
AGENT_CREATED_SPEC = _observation_spec(AGENT_CREATED, AgentLifecyclePayload)
AGENT_DISPOSED_SPEC = _observation_spec(AGENT_DISPOSED, AgentLifecyclePayload)
AGENT_ERROR_SPEC = _observation_spec(AGENT_ERROR, AgentLifecyclePayload)
AGENT_SESSION_START_SPEC = _observation_spec(AGENT_SESSION_START, AgentLifecyclePayload)
AGENT_STATUS_SPEC = _observation_spec(AGENT_STATUS, AgentLifecyclePayload)


__all__ = [
    "AGENT_AFTER_TURN_SPEC",
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
    "AfterTurnPayload",
    "AgentLifecyclePayload",
    "AgentRequestPayload",
    "AgentSubject",
    "AllowTurn",
    "BeforeReasoningPayload",
    "BeforeReasoningResult",
    "BeforeTurnPayload",
    "ContinueTurn",
    "RejectTurn",
    "RequestErrorPayload",
    "RetryRequest",
    "StopTurn",
    "TurnStoppedPayload",
    "TurnStoppingPayload",
]
