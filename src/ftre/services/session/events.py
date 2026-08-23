"""Stable names for optional session maintenance events.

The projection layer owns the generic session-event contract.  Optional Feature
packages may emit these names without importing one another or reaching into the
AgentLoop implementation.
"""
# 中文说明：Session 维护事件命名和 emit sink：可选 Feature 通过它通知 Projection，不持有 AgentLoop 引用。

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any


class SessionMaintenanceEvent(StrEnum):
    """CustomEvent names understood by :class:`SessionProjection`."""

    COMPACTION_START = "context_compact_start"
    COMPACTION_DONE = "context_compact_done"
    COMPACTION_FAILED = "context_compact_failed"


class SessionEventService:
    """可选 Feature 使用的稳定 Session 事件汇聚点。

    The sink is bound by the AgentLoop provider after the projection layer exists;
    feature packages can therefore emit projected events without importing or
    holding the AgentLoop itself.
    事件 sink 在 AgentLoop Provider 完成投影绑定前是 no-op，因此压缩、标题等可选
    Feature 可以先加载而不依赖 Loop；unbind 后再次 emit 也不会调用已释放对象。
    """

    def __init__(self) -> None:
        self._emitter: Callable[..., Awaitable[Any]] = self._noop

    async def _noop(self, *_args, **_kwargs) -> None:
        return None

    def bind(self, emitter: Callable[..., Awaitable[Any]]) -> Callable[[], None]:
        """绑定投影 emitter，并返回只撤销本次绑定的函数。"""
        if not callable(emitter):
            raise TypeError("session event emitter must be callable")
        previous = self._emitter
        self._emitter = emitter

        def unbind() -> None:
            if self._emitter is emitter:
                self._emitter = previous

        return unbind

    async def emit(self, *args, **kwargs) -> Any:
        """向当前 emitter 发出事件；未绑定时安全 no-op。"""
        return await self._emitter(*args, **kwargs)


__all__ = ["SessionEventService", "SessionMaintenanceEvent"]
