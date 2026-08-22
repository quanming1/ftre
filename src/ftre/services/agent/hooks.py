"""Agent 状态机语义 Hook 契约。

Hook payload 是进程内控制协议，不是 SessionEvent，也不进入 mailbox 持久化。
候选输入在 ``agent/pre-step`` 成功前仍然属于 pending；只有 ``EnterStep`` 被
接受后，SessionLane 才能 claim 它。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from ftre_agent_core.hooks import (
    AGENT_TURN_STOPPING_SPEC,
    ContinueTurn,
    StopTurn,
    TurnStoppingPayload,
)

from ftre.platform.hooks import (
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
    HookFailurePolicy,
    HookMode,
    HookScope,
    HookSpec,
)
from ftre.services.agent.config import AgentConfig
from ftre.services.session.entity.state import QueueItem


@dataclass(frozen=True, slots=True)
class AgentSubject:
    """Agent scope carrier；identity 必须跨生命周期唯一。"""

    agent_id: str
    identity: object


@dataclass(frozen=True, slots=True)
class PendingInput:
    """QueueItem 的只读 Hook 视图，不允许监听器回写 mailbox 对象。"""

    request_id: str
    sequence: int
    content: str
    attachments: tuple[Mapping[str, Any], ...]
    agent_id: str

    @classmethod
    def from_queue_item(cls, item: QueueItem) -> PendingInput:
        return cls(
            request_id=item.request_id,
            sequence=item.sequence,
            content=item.content,
            attachments=tuple(
                MappingProxyType(dict(attachment)) for attachment in item.attachments
            ),
            agent_id=item.agent_id,
        )


@dataclass(frozen=True, slots=True)
class PreStepPayload:
    agent: AgentSubject
    session_id: str
    turn_id: str
    candidate: PendingInput
    cancellation: asyncio.Event
    channel_id: str = ""
    config: AgentConfig | None = None
    set_maintenance: Callable[[bool, str], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class AfterTurnPayload:
    """Turn 完成后的可等待维护边界。

    ``set_maintenance`` 是 Lane 提供的通用状态桥接；Hook 可以标记自己的维护阶段，
    但不能访问 Mailbox 或改变 claim 顺序。压缩包因此可以拥有全部压缩逻辑，核心
    仍只拥有串行状态和公开快照。
    """

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
class EnterStep:
    candidate: PendingInput


@dataclass(frozen=True, slots=True)
class RejectStep:
    disposition: Literal["keep", "discard"]
    reason: str

    def __post_init__(self) -> None:
        if self.disposition not in {"keep", "discard"}:
            raise ValueError("RejectStep.disposition must be keep or discard")


@dataclass(frozen=True, slots=True)
class AgentRequestPayload:
    agent: AgentSubject
    session_id: str
    turn_id: str
    config: AgentConfig
    cancellation: asyncio.Event


@dataclass(frozen=True, slots=True)
class RequestErrorPayload:
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


@dataclass(frozen=True, slots=True)
class AgentInboxPayload:
    """Inbox mutation 的只读观察视图。"""

    agent: AgentSubject
    session_id: str
    item: PendingInput
    turn_id: str = ""
    reason: str = ""


async def _enter_step(payload: PreStepPayload) -> EnterStep:
    return EnterStep(payload.candidate)


async def _keep_request(payload: AgentRequestPayload) -> AgentConfig:
    return payload.config


async def _stop_on_error(payload: RequestErrorPayload) -> None:
    return None


AGENT_PRE_STEP_SPEC = HookSpec(
    AGENT_PRE_STEP,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=PreStepPayload,
    result_type=(EnterStep, RejectStep),
    default=_enter_step,
    scope=HookScope.AGENT,
)


async def _continue_after_turn(payload: AfterTurnPayload) -> None:
    return None


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


AGENT_TURN_STOPPED_SPEC = _observation_spec(
    AGENT_TURN_STOPPED, TurnStoppedPayload
)


AGENT_CREATED_SPEC = _observation_spec(AGENT_CREATED, AgentLifecyclePayload)
AGENT_DISPOSED_SPEC = _observation_spec(AGENT_DISPOSED, AgentLifecyclePayload)
AGENT_ERROR_SPEC = _observation_spec(AGENT_ERROR, AgentLifecyclePayload)
AGENT_SESSION_START_SPEC = _observation_spec(
    AGENT_SESSION_START, AgentLifecyclePayload
)
AGENT_STATUS_SPEC = _observation_spec(AGENT_STATUS, AgentLifecyclePayload)
AGENT_INBOX_INSERTED_SPEC = _observation_spec(
    AGENT_INBOX_INSERTED, AgentInboxPayload
)
AGENT_INBOX_CLAIMED_SPEC = _observation_spec(AGENT_INBOX_CLAIMED, AgentInboxPayload)
AGENT_INBOX_DISCARDED_SPEC = _observation_spec(
    AGENT_INBOX_DISCARDED, AgentInboxPayload
)


__all__ = [
    "AGENT_AFTER_TURN_SPEC",
    "AGENT_CREATED_SPEC",
    "AGENT_DISPOSED_SPEC",
    "AGENT_ERROR_SPEC",
    "AGENT_INBOX_CLAIMED_SPEC",
    "AGENT_INBOX_DISCARDED_SPEC",
    "AGENT_INBOX_INSERTED_SPEC",
    "AGENT_PRE_STEP_SPEC",
    "AGENT_REQUEST_ERROR_SPEC",
    "AGENT_REQUEST_SPEC",
    "AGENT_SESSION_START_SPEC",
    "AGENT_STATUS_SPEC",
    "AGENT_TURN_STOPPED_SPEC",
    "AGENT_TURN_STOPPING_SPEC",
    "AfterTurnPayload",
    "AgentInboxPayload",
    "AgentLifecyclePayload",
    "AgentRequestPayload",
    "AgentSubject",
    "ContinueTurn",
    "EnterStep",
    "PendingInput",
    "PreStepPayload",
    "RejectStep",
    "RequestErrorPayload",
    "RetryRequest",
    "StopTurn",
    "TurnStoppedPayload",
    "TurnStoppingPayload",
]
