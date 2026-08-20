"""Public AgentService facade; the AgentLoop remains an internal provider detail."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


class AgentService:
    key = "agents"

    def __init__(self, loop: Any | None = None, profiles: Any | None = None) -> None:
        self._loop = loop
        self._profiles = profiles
        self._listeners: dict[str, list[Callable[..., Any]]] = {"created": [], "disposed": []}

    @property
    def loop(self) -> Any:
        if self._loop is None:
            raise RuntimeError("AgentService runtime is not ready")
        return self._loop

    def bind(self, loop: Any, profiles: Any | None = None) -> None:
        self._loop = loop
        if profiles is not None:
            self._profiles = profiles

    async def submit(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("submit", *args, **kwargs)

    async def cancel(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("cancel", *args, **kwargs)

    async def wait(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("wait", *args, **kwargs)

    def status(self, session_id: str) -> Any:
        if self._loop is None:
            return "idle"
        return self._loop.get_session_status(session_id)

    def is_busy(self, session_id: str) -> bool:
        return self.status(session_id) in {"running", "processing", "compacting"}

    def list(self) -> list[dict[str, Any]]:
        return [{"id": "default", "state": "ready"}] if self._loop is not None else []

    def get(self, agent_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list() if item["id"] == agent_id), None)

    def tool_scope(self, agent_id: str) -> str:
        return f"agent:{agent_id}"

    def on_created(self, callback: Callable[..., Any]):
        return self._listen("created", callback)

    def on_disposed(self, callback: Callable[..., Any]):
        return self._listen("disposed", callback)

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        target = getattr(self.loop, name, None)
        if target is None:
            raise AttributeError(f"AgentLoop has no public operation {name!r}")
        result = target(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def _listen(self, event: str, callback: Callable[..., Any]):
        self._listeners[event].append(callback)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._listeners[event].remove(callback)
            except ValueError:
                return False
            return True

        return dispose

