"""Agent Service 的公开契约。

这些 Protocol 是 Gateway、HTTP、Channel 和 Feature 可依赖的边界；它们不暴露
Inbox、TurnExecutor 或 AgentLoop 对象。具体数据面由 agent_loop Provider 实现
``AgentDriver``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """已经归一化、准备交给 AgentService 执行的一条输入。

    这是 AgentService 的唯一数据面输入。它不包含 QueueItem、pending、容量或
    客户端队列状态；可选 Inbox Package 负责决定何时生成它。
    """

    session_id: str
    request_id: str
    channel_id: str
    content: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    source: str = "user"
    metadata: dict[str, Any] | None = None


@runtime_checkable
class AgentDriver(Protocol):
    """Agent Service 所需的最小运行时端口。"""

    def is_session_busy(self, session_id: str) -> bool: ...

    def get_session_status(self, session_id: str) -> str: ...

    def is_busy(self, session_id: str) -> bool: ...

    def run(self, message: InboundMessage) -> Awaitable[Any]: ...

    def cancel(self, *args: Any, **kwargs: Any) -> Awaitable[Any]: ...

    def delete_session(self, session_id: str) -> Awaitable[Any]: ...

    def resume_confirmation(
        self,
        session_id: str,
        channel_id: str,
        events: list[Any],
        metadata: Any,
    ) -> Awaitable[Any]: ...



@runtime_checkable
class AgentRegistryProtocol(Protocol):
    """Agent 身份和 scope 的查询边界。"""

    def list(self) -> list[dict[str, Any]]: ...

    def get(self, agent_id: str) -> dict[str, Any] | None: ...

    def tool_scope(self, agent_id: str) -> str: ...

    def scope_identity(self, agent_id: str) -> object: ...


AgentListener = Callable[[dict[str, Any]], Any]

__all__ = ["AgentDriver", "AgentListener", "AgentRegistryProtocol", "InboundMessage"]
