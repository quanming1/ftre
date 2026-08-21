"""Agent Service 的公开契约。

这些 Protocol 是 Gateway、HTTP、Channel 和 Feature 可依赖的边界；它们不暴露
SessionLane、TurnExecutor 或 AgentLoop 对象。具体数据面由 agent_loop Provider
实现 ``AgentDriver``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentDriver(Protocol):
    """Agent Service 所需的最小运行时端口。"""

    def is_session_busy(self, session_id: str) -> bool: ...

    def get_session_status(self, session_id: str) -> str: ...

    def submit(self, *args: Any, **kwargs: Any) -> Awaitable[Any]: ...

    def cancel(self, *args: Any, **kwargs: Any) -> Awaitable[Any]: ...

    def wait(self, *args: Any, **kwargs: Any) -> Awaitable[Any]: ...

    def delete_session(self, session_id: str) -> Awaitable[Any]: ...

    def cancel_queued_message(
        self, session_id: str, request_id: str
    ) -> Awaitable[Any]: ...

    def get_mailbox_snapshot(self, session_id: str) -> Awaitable[Any]: ...

    def resume_confirmation(
        self,
        session_id: str,
        channel_id: str,
        events: list[Any],
        metadata: Any,
    ) -> Awaitable[Any]: ...

    def wait_session_quiescent(self, session_id: str) -> Awaitable[Any]: ...


@runtime_checkable
class AgentRegistryProtocol(Protocol):
    """Agent 身份和 scope 的查询边界。"""

    def list(self) -> list[dict[str, Any]]: ...

    def get(self, agent_id: str) -> dict[str, Any] | None: ...

    def tool_scope(self, agent_id: str) -> str: ...

    def scope_identity(self, agent_id: str) -> object: ...


AgentListener = Callable[[dict[str, Any]], Any]

__all__ = ["AgentDriver", "AgentListener", "AgentRegistryProtocol"]
