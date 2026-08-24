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
    "INBOX_BEFORE_CLAIM",
    "INBOX_BEFORE_CLAIM_SPEC",
    "INBOX_CHANGED",
    "INBOX_CHANGED_SPEC",
    "INBOX_STATUS_CHANGED",
    "INBOX_STATUS_CHANGED_SPEC",
    "BeforeClaimPayload",
    "EnterClaim",
    "InboxChangedPayload",
    "InboxStatusPayload",
    "RejectClaim",
]
