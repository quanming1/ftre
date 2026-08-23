"""Inbox 生命周期 Hook。

该 Hook 只描述“领取前是否允许交付”，不让 AgentService 知道队列，也不让监听器
直接修改 Repository。Compaction 等可选包可以在这里阻止 claim 并保留 pending。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

try:  # ftre Gateway 运行时提供真实 HookSpec；独立包导入不应强依赖 Gateway。
    from ftre.platform.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec
except ModuleNotFoundError:  # pragma: no cover - 仅用于独立 wheel 的 import smoke
    HookSpec = None  # type: ignore[assignment,misc]
    HookFailurePolicy = HookMode = HookScope = None  # type: ignore[assignment,misc]

from .models import QueueItem, QueueTarget

INBOX_BEFORE_CLAIM = "inbox/before-claim"
INBOX_INSERTED = "inbox/inserted"
INBOX_CLAIMED = "inbox/claimed"
INBOX_DISCARDED = "inbox/discarded"
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
class InboxMutationPayload:
    """队列 mutation 的只读观察视图。"""

    session_id: str
    item: QueueItem
    target: QueueTarget
    operation: Literal["inserted", "claimed", "discarded"]


@dataclass(frozen=True, slots=True)
class InboxChangedPayload:
    """队列事实已持久化，可供协议适配器读取最新权威快照。"""

    session_id: str


@dataclass(frozen=True, slots=True)
class InboxStatusPayload:
    """Inbox 自有阻塞/空闲状态变化，不混入 queue snapshot。"""

    session_id: str
    status: str


def _observe(_payload: InboxMutationPayload) -> None:
    return None


async def _enter(payload: BeforeClaimPayload) -> EnterClaim:
    return EnterClaim(payload.candidate.request_id)


INBOX_BEFORE_CLAIM_SPEC = (
    HookSpec(
        INBOX_BEFORE_CLAIM,
        "inbox",
        HookMode.WATERFALL,
        failure_policy=HookFailurePolicy.PROPAGATE,
        payload_type=BeforeClaimPayload,
        result_type=(EnterClaim, RejectClaim),
        default=_enter,
        scope=HookScope.GLOBAL,
    )
    if HookSpec is not None
    else None
)

if HookSpec is not None:
    def _observe_spec(name: str):
        return HookSpec(
            name,
            "inbox",
            HookMode.EMIT,
            failure_policy=HookFailurePolicy.OBSERVE,
            payload_type=InboxMutationPayload,
            result_type=type(None),
            default=_observe,
            scope=HookScope.GLOBAL,
        )

    INBOX_INSERTED_SPEC = _observe_spec(INBOX_INSERTED)
    INBOX_CLAIMED_SPEC = _observe_spec(INBOX_CLAIMED)
    INBOX_DISCARDED_SPEC = _observe_spec(INBOX_DISCARDED)
    INBOX_CHANGED_SPEC = HookSpec(
        INBOX_CHANGED,
        "inbox",
        HookMode.EMIT,
        failure_policy=HookFailurePolicy.OBSERVE,
        payload_type=InboxChangedPayload,
        result_type=type(None),
        default=lambda _payload: None,
        scope=HookScope.GLOBAL,
    )
    INBOX_STATUS_CHANGED_SPEC = HookSpec(
        INBOX_STATUS_CHANGED,
        "inbox",
        HookMode.EMIT,
        failure_policy=HookFailurePolicy.OBSERVE,
        payload_type=InboxStatusPayload,
        result_type=type(None),
        default=lambda _payload: None,
        scope=HookScope.GLOBAL,
    )
else:  # pragma: no cover - independent import fallback
    INBOX_INSERTED_SPEC = INBOX_CLAIMED_SPEC = INBOX_DISCARDED_SPEC = None
    INBOX_CHANGED_SPEC = INBOX_STATUS_CHANGED_SPEC = None

__all__ = [
    "INBOX_BEFORE_CLAIM",
    "INBOX_BEFORE_CLAIM_SPEC",
    "INBOX_CHANGED",
    "INBOX_CHANGED_SPEC",
    "INBOX_CLAIMED",
    "INBOX_CLAIMED_SPEC",
    "INBOX_DISCARDED",
    "INBOX_DISCARDED_SPEC",
    "INBOX_INSERTED",
    "INBOX_INSERTED_SPEC",
    "INBOX_STATUS_CHANGED",
    "INBOX_STATUS_CHANGED_SPEC",
    "BeforeClaimPayload",
    "EnterClaim",
    "InboxChangedPayload",
    "InboxMutationPayload",
    "InboxStatusPayload",
    "RejectClaim",
]
