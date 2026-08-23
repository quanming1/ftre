"""Session post-commit and persistence-barrier Hook contracts."""
# 中文说明：Session lifecycle/flush Hook 数据契约：描述提交后通知和持久化屏障，不负责执行 Hook。

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ftre.kernel.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec

# SessionService owns persistence lifecycle names; they are not Kernel concepts.
SESSION_CREATED = "session/created"
SESSION_DISPOSED = "session/disposed"
SESSION_EVENT = "session/event"
SESSION_FLUSH = "session/flush"


@dataclass(frozen=True, slots=True)
class SessionEventPayload:
    """A committed Session event; observers run after projection persistence."""

    session_id: str
    event: object
    persisted_ids: tuple[str, ...] = ()
    completed_id: str = ""


@dataclass(frozen=True, slots=True)
class SessionFlushPayload:
    """Explicit persistence barrier input."""

    session_id: str
    reason: str
    cancellation: asyncio.Event


@dataclass(frozen=True, slots=True)
class SessionLifecyclePayload:
    """Session identity committed to or removed from durable storage."""

    session_id: str
    channel_id: str = ""
    reason: str = ""


def _observe(_payload: SessionEventPayload) -> None:
    return None


def _flush(_payload: SessionFlushPayload) -> None:
    return None


def _lifecycle(_payload: SessionLifecyclePayload) -> None:
    return None


def _lifecycle_spec(name: str) -> HookSpec:
    return HookSpec(
        name,
        "session",
        HookMode.EMIT,
        failure_policy=HookFailurePolicy.OBSERVE,
        payload_type=SessionLifecyclePayload,
        result_type=type(None),
        default=_lifecycle,
        scope=HookScope.GLOBAL,
    )


SESSION_CREATED_SPEC = _lifecycle_spec(SESSION_CREATED)
SESSION_DISPOSED_SPEC = _lifecycle_spec(SESSION_DISPOSED)


SESSION_EVENT_SPEC = HookSpec(
    SESSION_EVENT,
    "session",
    HookMode.EMIT,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=SessionEventPayload,
    result_type=type(None),
    default=_observe,
    scope=HookScope.GLOBAL,
)

SESSION_FLUSH_SPEC = HookSpec(
    SESSION_FLUSH,
    "session",
    HookMode.PARALLEL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=SessionFlushPayload,
    result_type=type(None),
    default=_flush,
    scope=HookScope.GLOBAL,
)


__all__ = [
    "SESSION_CREATED_SPEC",
    "SESSION_DISPOSED_SPEC",
    "SESSION_EVENT_SPEC",
    "SESSION_FLUSH_SPEC",
    "SessionEventPayload",
    "SessionFlushPayload",
    "SessionLifecyclePayload",
]
