"""Agent 公共 Service：身份、状态和 Runtime Factory 调度。

``ftre-agent`` 是公开的 Service Owner。具体 AgentLoop 由 Runtime Provider
注册到这里，但不会反过来创建或发布 ``agents`` Service。这样 Gateway、Inbox、
HTTP 和 Channel 只依赖本模块，不会看到 Runtime 的具体实现。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AgentListener,
    AgentRunResult,
    AgentRuntimeFactory,
    InboundMessage,
)
from .errors import (
    FactoryAlreadyRegisteredError,
    FactoryNotRegisteredError,
    FactoryRegistrationMismatchError,
    InvalidFactoryError,
    ServiceClosedError,
)
from .registry import AgentRegistry, HookScopeCarrier


@dataclass(frozen=True, slots=True)
class FactoryRegistration:
    """一次 Runtime Factory 注册的不可变句柄。"""

    token: object
    name: str


def _factory_name(factory: Any) -> str:
    return str(getattr(factory, "name", None) or factory.__class__.__name__)


class AgentService:
    """对外稳定的 Agent 合约和唯一 ``agents`` Service Owner。"""

    key = "agents"

    def __init__(self) -> None:
        self._factory: Any = None
        self._factory_registration: FactoryRegistration | None = None
        self._closed = False
        self.registry = AgentRegistry()
        self._listeners: dict[str, list[AgentListener]] = {
            "created": [],
            "disposed": [],
        }

    def start(self) -> None:
        """允许 Composition 在提供 Service 后显式启动它。"""
        self._closed = False

    def close(self) -> None:
        """关闭 Service；Runtime Factory 的生命周期由其 Provider 管理。"""
        self._closed = True
        self._factory = None
        self._factory_registration = None
        for record in tuple(self.registry.list()):
            self.registry.dispose(record["id"])

    def register_factory(self, factory: AgentRuntimeFactory) -> FactoryRegistration:
        """注册唯一 Runtime Factory，不暴露第二个 Context Service。"""
        if self._closed:
            raise ServiceClosedError("AgentService is closed")
        if self._factory is not None:
            raise FactoryAlreadyRegisteredError(
                "AgentService already has a registered factory"
            )
        required = (
            "run_inbound",
            "cancel_session",
            "get_session_status",
            "is_active_session",
            "delete_session",
            "resume_confirmation",
        )
        missing = [name for name in required if not callable(getattr(factory, name, None))]
        if missing:
            raise InvalidFactoryError(
                f"invalid Agent Runtime Factory; missing: {', '.join(missing)}"
            )
        registration = FactoryRegistration(token=object(), name=_factory_name(factory))
        self._factory = factory
        self._factory_registration = registration
        if self.registry.get("default") is None:
            self.registry.register("default", state="ready")
        return registration

    def unregister_factory(self, registration: FactoryRegistration) -> bool:
        """按注册句柄摘除 Runtime Factory；重复摘除安全。"""
        if self._factory_registration is None:
            return False
        if registration.token is not self._factory_registration.token:
            raise FactoryRegistrationMismatchError(
                "Agent Runtime Factory registration does not match"
            )
        self._factory = None
        self._factory_registration = None
        for record in tuple(self.registry.list()):
            self.registry.dispose(record["id"])
        return True

    @property
    def factory_name(self) -> str | None:
        """只提供诊断名称，不暴露 Runtime 对象。"""
        return self._factory_registration.name if self._factory_registration else None

    def is_ready(self) -> bool:
        return not self._closed and self._factory is not None

    def _factory_or_raise(self) -> Any:
        if self._closed:
            raise ServiceClosedError("AgentService is closed")
        if self._factory is None:
            raise FactoryNotRegisteredError("AgentService Runtime Factory is not ready")
        return self._factory

    async def run(self, message: InboundMessage) -> AgentRunResult:
        """执行一条已经由上游交付的 InboundMessage。"""
        return await self._await(self._factory_or_raise().run_inbound(message))

    async def cancel(self, *args: Any, **kwargs: Any) -> Any:
        """请求 Runtime 取消会话中的 active Turn；未交付输入由 InboxService 负责。"""
        return await self._await(self._factory_or_raise().cancel_session(*args, **kwargs))

    def status(self, session_id: str) -> str:
        """查询 Session 当前状态；Runtime 未绑定时返回 idle。"""
        if self._factory is None:
            return "idle"
        return self._factory.get_session_status(session_id)

    def is_busy(self, session_id: str) -> bool:
        """判断 Session 是否仍有活动 Turn 或维护任务。"""
        return self.status(session_id) in {"running", "processing", "compacting"}

    def get_session_status(self, session_id: str) -> str:
        """公开状态查询命名，与 Runtime 的 ``get_session_status`` 对齐。"""
        return self.status(session_id)

    def is_session_busy(self, session_id: str) -> bool:
        """公开忙碌查询命名；语义与 ``is_busy`` 一致。"""
        return self.is_busy(session_id)

    async def delete_session(self, session_id: str) -> Any:
        """请求 Runtime 关闭并删除一个 Session。"""
        return await self._await(self._factory_or_raise().delete_session(session_id))

    async def resume_confirmation(
        self,
        session_id: str,
        channel_id: str,
        events: list[Any],
        metadata: Any,
    ) -> Any:
        """Apply existing confirmation events and resume the paused Agent turn."""
        return await self._await(
            self._factory_or_raise().resume_confirmation(
                session_id,
                channel_id,
                events,
                metadata,
            )
        )

    def list(self) -> list[dict[str, Any]]:
        """返回 Agent registry 的诊断摘要。"""
        return self.registry.list()

    def get(self, agent_id: str) -> dict[str, Any] | None:
        """读取一个 Agent 的公开状态摘要。"""
        return self.registry.get(agent_id)

    def tool_scope(self, agent_id: str) -> str:
        """返回该 Agent 的 scoped tool key。"""
        return self.registry.tool_scope(agent_id)

    def scope_identity(self, agent_id: str) -> object:
        """返回当前 Agent 生命周期专用的 scope identity。"""
        return self.registry.scope_identity(agent_id)

    def scope_carrier(
        self, agent_id: str, *, parent_id: str | None = None
    ) -> HookScopeCarrier:
        """构造传给 HookRuntime 的 Agent scope carrier。"""
        return self.registry.scope_carrier(agent_id, parent_id=parent_id)

    def on_created(self, callback: AgentListener) -> Callable[[], bool]:
        """订阅 Agent 创建事件，并返回幂等取消函数。"""
        return self._listen("created", callback)

    def on_disposed(self, callback: AgentListener) -> Callable[[], bool]:
        """订阅 Agent 销毁事件，并返回幂等取消函数。"""
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
