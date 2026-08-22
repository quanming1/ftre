"""Stable names for optional session maintenance events.

The projection layer owns the generic session-event contract.  Optional Feature
packages may emit these names without importing one another or reaching into the
AgentLoop implementation.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any


class SessionMaintenanceEvent(StrEnum):
    """CustomEvent names understood by :class:`SessionProjection`."""

    COMPACTION_START = "context_compact_start"
    COMPACTION_DONE = "context_compact_done"
    COMPACTION_FAILED = "context_compact_failed"


class SessionEventService:
    """Stable event sink for optional feature packages.

    The sink is bound by the AgentLoop provider after the projection layer exists;
    feature packages can therefore emit projected events without importing or
    holding the AgentLoop itself.
    """

    def __init__(self) -> None:
        self._emitter: Callable[..., Awaitable[Any]] = self._noop

    async def _noop(self, *_args, **_kwargs) -> None:
        return None

    def bind(self, emitter: Callable[..., Awaitable[Any]]) -> Callable[[], None]:
        if not callable(emitter):
            raise TypeError("session event emitter must be callable")
        previous = self._emitter
        self._emitter = emitter

        def unbind() -> None:
            if self._emitter is emitter:
                self._emitter = previous

        return unbind

    async def emit(self, *args, **kwargs) -> Any:
        return await self._emitter(*args, **kwargs)


__all__ = ["SessionEventService", "SessionMaintenanceEvent"]
