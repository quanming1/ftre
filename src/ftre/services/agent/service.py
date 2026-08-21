"""Public Agent Service: Registry + explicit AgentDriver port.

AgentService owns Agent identity and public operations. It never exposes an
``AgentLoop`` attribute and never performs generic method-name forwarding; the
independent ``services.agent_loop`` Provider attaches an explicit AgentDriver.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from ftre.platform.hooks import HookScopeCarrier

from .contracts import AgentDriver, AgentListener
from .registry import AgentRegistry


class AgentService:
    """Stable Agent contract consumed by HTTP/WS/Feature code."""

    key = "agents"

    def __init__(self, profiles: Any | None = None) -> None:
        self._profiles = profiles
        self._driver: AgentDriver | None = None
        self.registry = AgentRegistry()
        self._listeners: dict[str, list[AgentListener]] = {
            "created": [],
            "disposed": [],
        }

    @property
    def driver(self) -> AgentDriver:
        """Return the attached runtime port, never the concrete AgentLoop."""
        if self._driver is None:
            raise RuntimeError("AgentService runtime is not ready")
        return self._driver

    def attach_driver(self, driver: AgentDriver, profiles: Any | None = None) -> None:
        """Attach an explicit data-plane port after Provider composition."""
        if not isinstance(driver, AgentDriver):
            raise TypeError("driver must implement AgentDriver")
        if self._driver is not None and self._driver is not driver:
            raise RuntimeError("AgentService already has an attached driver")
        self._driver = driver
        if profiles is not None:
            self._profiles = profiles
        if self.registry.get("default") is None:
            self.registry.register("default", state="ready")

    def detach_driver(self) -> None:
        """Detach the provider during Gateway shutdown; safe to repeat."""
        self._driver = None
        for record in tuple(self.registry.list()):
            self.registry.dispose(record["id"])

    async def submit(self, *args: Any, **kwargs: Any) -> Any:
        return await self._await(self.driver.submit(*args, **kwargs))

    async def cancel(self, *args: Any, **kwargs: Any) -> Any:
        return await self._await(self.driver.cancel(*args, **kwargs))

    async def wait(self, *args: Any, **kwargs: Any) -> Any:
        return await self._await(self.driver.wait(*args, **kwargs))

    def status(self, session_id: str) -> str:
        if self._driver is None:
            return "idle"
        return self._driver.get_session_status(session_id)

    def is_busy(self, session_id: str) -> bool:
        return self.status(session_id) in {"running", "processing", "compacting"}

    def get_session_status(self, session_id: str) -> str:
        return self.status(session_id)

    def is_session_busy(self, session_id: str) -> bool:
        return self.is_busy(session_id)

    async def delete_session(self, session_id: str) -> Any:
        return await self._await(self.driver.delete_session(session_id))

    async def cancel_queued_message(self, session_id: str, request_id: str) -> Any:
        return await self._await(
            self.driver.cancel_queued_message(session_id, request_id)
        )

    async def get_mailbox_snapshot(self, session_id: str) -> Any:
        return await self._await(self.driver.get_mailbox_snapshot(session_id))

    async def resume_confirmation(
        self,
        session_id: str,
        channel_id: str,
        events: list[Any],
        metadata: Any,
    ) -> Any:
        """Apply existing confirmation events and resume the paused Agent turn."""
        return await self._await(
            self.driver.resume_confirmation(
                session_id,
                channel_id,
                events,
                metadata,
            )
        )

    async def wait_session_quiescent(self, session_id: str) -> Any:
        return await self._await(self.driver.wait_session_quiescent(session_id))

    def list(self) -> list[dict[str, Any]]:
        return self.registry.list()

    def get(self, agent_id: str) -> dict[str, Any] | None:
        return self.registry.get(agent_id)

    def tool_scope(self, agent_id: str) -> str:
        return self.registry.tool_scope(agent_id)

    def scope_identity(self, agent_id: str) -> object:
        return self.registry.scope_identity(agent_id)

    def scope_carrier(
        self, agent_id: str, *, parent_id: str | None = None
    ) -> HookScopeCarrier:
        return self.registry.scope_carrier(agent_id, parent_id=parent_id)

    def on_created(self, callback: AgentListener) -> Callable[[], bool]:
        return self._listen("created", callback)

    def on_disposed(self, callback: AgentListener) -> Callable[[], bool]:
        return self._listen("disposed", callback)

    @staticmethod
    async def _await(result: Any) -> Any:
        return await result if inspect.isawaitable(result) else result

    def _listen(self, event: str, callback: AgentListener) -> Callable[[], bool]:
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


__all__ = ["AgentService"]
