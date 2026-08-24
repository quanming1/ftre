"""Session 生命周期 Hook contracts."""
# 中文说明：这里只公开 Session 创建/删除后的事实通知；持久化 flush 是 Service 内部实现，
# 没有真实跨 Owner 消费者时不预留公共 Hook。

from __future__ import annotations

from dataclasses import dataclass

from ftre.kernel.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec

# SessionService owns persistence lifecycle names; they are not Kernel concepts.
SESSION_CREATED = "session/created"
SESSION_DISPOSED = "session/disposed"
@dataclass(frozen=True, slots=True)
class SessionLifecyclePayload:
    """Session identity committed to or removed from durable storage."""

    session_id: str
    channel_id: str = ""
    reason: str = ""


def _lifecycle(_payload: SessionLifecyclePayload) -> None:
    return None


def _lifecycle_spec(name: str) -> HookSpec:
    return HookSpec(
        name,
        "session",
        HookMode.PARALLEL,
        failure_policy=HookFailurePolicy.OBSERVE,
        payload_type=SessionLifecyclePayload,
        result_type=type(None),
        default=_lifecycle,
        scope=HookScope.GLOBAL,
    )


SESSION_CREATED_SPEC = _lifecycle_spec(SESSION_CREATED)
SESSION_DISPOSED_SPEC = _lifecycle_spec(SESSION_DISPOSED)


__all__ = [
    "SESSION_CREATED_SPEC",
    "SESSION_DISPOSED_SPEC",
    "SessionLifecyclePayload",
]
