"""Agent 运行时 Hook 契约。

这里仅描述 Agent active Run 的语义：Run 准入、运行错误和 Run 收尾
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
    AGENT_STOP_DECISION_SPEC,
    BeforeReasoningPayload,
    BeforeReasoningResult,
    ContinueTurn,
    StopDecisionPayload,
    StopTurn,
)

from ftre.kernel.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec
from ftre.services.agent.config import AgentConfig

# Agent Service owns lifecycle and active-turn names; Kernel only dispatches them.
AGENT_BEFORE_RUN = "agent/before-run"
AGENT_AFTER_RUN = "agent/after-run"
AGENT_RUN_ERROR = "agent/run-error"
AGENT_BEFORE_REASONING = AGENT_BEFORE_REASONING_SPEC.name
AGENT_STOP_DECISION = AGENT_STOP_DECISION_SPEC.name


@dataclass(frozen=True, slots=True)
class AgentSubject:
    """Agent scope carrier；identity 必须跨生命周期唯一。"""

    agent_id: str
    identity: object


@dataclass(frozen=True, slots=True)
class BeforeRunPayload:
    """一条已交付 InboundMessage 进入 Agent Run 前的准入输入。"""

    agent: AgentSubject
    session_id: str
    turn_id: str
    cancellation: asyncio.Event
    channel_id: str = ""
    config: AgentConfig | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AllowRun:
    """默认允许本次 InboundMessage 创建 active Run。"""

    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RejectRun:
    """阻止本次 InboundMessage 进入 active Run；pending 仍由 Inbox 持有。"""

    reason: str


@dataclass(frozen=True, slots=True)
class AfterRunPayload:
    """Run 完成后的可等待维护边界。"""

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
class RequestErrorPayload:
    """一次 Agent Run 失败时传给错误 Hook 的结构化上下文。"""

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


async def _allow_run(payload: BeforeRunPayload) -> AllowRun:
    return AllowRun(payload.context)


async def _stop_on_error(payload: RequestErrorPayload) -> None:
    return None


async def _continue_after_run(payload: AfterRunPayload) -> None:
    return None

AGENT_BEFORE_RUN_SPEC = HookSpec(
    AGENT_BEFORE_RUN,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=BeforeRunPayload,
    result_type=(AllowRun, RejectRun),
    default=_allow_run,
    scope=HookScope.AGENT,
)

AGENT_AFTER_RUN_SPEC = HookSpec(
    AGENT_AFTER_RUN,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=AfterRunPayload,
    result_type=type(None),
    default=_continue_after_run,
    scope=HookScope.AGENT,
)

AGENT_RUN_ERROR_SPEC = HookSpec(
    AGENT_RUN_ERROR,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=RequestErrorPayload,
    result_type=(RetryRequest, type(None)),
    default=_stop_on_error,
    scope=HookScope.AGENT,
)

__all__ = [
    "AGENT_AFTER_RUN_SPEC",
    "AGENT_BEFORE_REASONING_SPEC",
    "AGENT_BEFORE_RUN_SPEC",
    "AGENT_RUN_ERROR_SPEC",
    "AGENT_STOP_DECISION_SPEC",
    "AfterRunPayload",
    "AgentSubject",
    "AllowRun",
    "BeforeReasoningPayload",
    "BeforeReasoningResult",
    "BeforeRunPayload",
    "ContinueTurn",
    "RejectRun",
    "RequestErrorPayload",
    "RetryRequest",
    "StopDecisionPayload",
    "StopTurn",
]
