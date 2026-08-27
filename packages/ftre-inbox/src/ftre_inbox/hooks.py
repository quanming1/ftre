"""Inbox 生命周期 Hook。

该 Hook 只描述“领取前是否允许交付”，不让 AgentService 知道队列，也不让监听器
直接修改 Repository。Compaction 等可选包可以在这里阻止 claim 并保留 pending。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from ftre.kernel.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec

from .models import QueueItem, QueueTarget

INBOX_BEFORE_CLAIM = "inbox/before-claim"
INBOX_ADMITTED = "inbox/admitted"
INBOX_CLAIMED = "inbox/claimed"
INBOX_DEFERRED = "inbox/deferred"
INBOX_DELIVERED = "inbox/delivered"
INBOX_ERROR = "inbox/error"
INBOX_CHANGED = "inbox/changed"
INBOX_STATUS_CHANGED = "inbox/status-changed"


@dataclass(frozen=True, slots=True)
class BeforeClaimPayload:
    session_id: str
    candidate: QueueItem
    target: QueueTarget
    channel_id: str
    cancellation: asyncio.Event
    candidates: tuple[QueueItem, ...] = ()


@dataclass(frozen=True, slots=True)
class EnterClaim:
    request_id: str


@dataclass(frozen=True, slots=True)
class RejectClaim:
    disposition: Literal["keep", "discard"]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class InboxChangedPayload:
    """队列事实已持久化，可供协议适配器读取最新权威快照。"""

    session_id: str


@dataclass(frozen=True, slots=True)
class InboxStatusPayload:
    """Inbox 自有阻塞/空闲状态变化，不混入 queue snapshot。"""

    session_id: str
    status: str


@dataclass(frozen=True, slots=True)
class InboxAdmissionPayload:
    session_id: str
    request_id: str
    target: QueueTarget
    item: QueueItem
    created: bool


@dataclass(frozen=True, slots=True)
class InboxClaimedPayload:
    session_id: str
    request_ids: tuple[str, ...]
    lease_id: str


@dataclass(frozen=True, slots=True)
class InboxDeferredPayload:
    session_id: str
    request_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class InboxDeliveredPayload:
    session_id: str
    request_id: str
    lease_id: str
    status: str


@dataclass(frozen=True, slots=True)
class InboxErrorPayload:
    session_id: str
    request_id: str
    stage: str
    error: str
    retryable: bool = True


async def _enter(payload: BeforeClaimPayload) -> EnterClaim:
    return EnterClaim(payload.candidate.request_id)


INBOX_BEFORE_CLAIM_SPEC = HookSpec(
    INBOX_BEFORE_CLAIM,
    "inbox",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=BeforeClaimPayload,
    result_type=(EnterClaim, RejectClaim),
    default=_enter,
    scope=HookScope.GLOBAL,
)

INBOX_CHANGED_SPEC = HookSpec(
    INBOX_CHANGED,
    "inbox",
    HookMode.PARALLEL,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=InboxChangedPayload,
    result_type=type(None),
    default=lambda _payload: None,
    scope=HookScope.GLOBAL,
)


def _observe(_payload):
    return None


INBOX_ADMITTED_SPEC = HookSpec(
    INBOX_ADMITTED,
    "inbox",
    HookMode.PARALLEL,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=InboxAdmissionPayload,
    result_type=type(None),
    default=_observe,
    scope=HookScope.GLOBAL,
)
INBOX_CLAIMED_SPEC = HookSpec(
    INBOX_CLAIMED,
    "inbox",
    HookMode.PARALLEL,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=InboxClaimedPayload,
    result_type=type(None),
    default=_observe,
    scope=HookScope.GLOBAL,
)
INBOX_DEFERRED_SPEC = HookSpec(
    INBOX_DEFERRED,
    "inbox",
    HookMode.PARALLEL,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=InboxDeferredPayload,
    result_type=type(None),
    default=_observe,
    scope=HookScope.GLOBAL,
)
INBOX_DELIVERED_SPEC = HookSpec(
    INBOX_DELIVERED,
    "inbox",
    HookMode.PARALLEL,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=InboxDeliveredPayload,
    result_type=type(None),
    default=_observe,
    scope=HookScope.GLOBAL,
)
INBOX_ERROR_SPEC = HookSpec(
    INBOX_ERROR,
    "inbox",
    HookMode.PARALLEL,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=InboxErrorPayload,
    result_type=type(None),
    default=_observe,
    scope=HookScope.GLOBAL,
)
INBOX_STATUS_CHANGED_SPEC = HookSpec(
    INBOX_STATUS_CHANGED,
    "inbox",
    HookMode.PARALLEL,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=InboxStatusPayload,
    result_type=type(None),
    default=lambda _payload: None,
    scope=HookScope.GLOBAL,
)

__all__ = [
    "INBOX_ADMITTED",
    "INBOX_ADMITTED_SPEC",
    "INBOX_BEFORE_CLAIM",
    "INBOX_BEFORE_CLAIM_SPEC",
    "INBOX_CHANGED",
    "INBOX_CHANGED_SPEC",
    "INBOX_CLAIMED",
    "INBOX_CLAIMED_SPEC",
    "INBOX_DEFERRED",
    "INBOX_DEFERRED_SPEC",
    "INBOX_DELIVERED",
    "INBOX_DELIVERED_SPEC",
    "INBOX_ERROR",
    "INBOX_ERROR_SPEC",
    "INBOX_STATUS_CHANGED",
    "INBOX_STATUS_CHANGED_SPEC",
    "BeforeClaimPayload",
    "EnterClaim",
    "InboxAdmissionPayload",
    "InboxChangedPayload",
    "InboxClaimedPayload",
    "InboxDeferredPayload",
    "InboxDeliveredPayload",
    "InboxErrorPayload",
    "InboxStatusPayload",
    "RejectClaim",
]
