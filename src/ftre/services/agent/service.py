"""Agent 公共 Service：身份注册 + 显式数据面 Driver。

这里是 HTTP、WebSocket 和 Feature 看到的 Agent 边界。它只保存 Agent 的公开
身份/状态，并把执行请求交给 ``AgentDriver``；真正的 AgentLoop 由独立 Provider
在 Composition 阶段组装，避免业务调用方反向依赖 Loop 的私有实现。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from ftre.kernel.hooks import HookScopeCarrier

from .contracts import AgentDriver, AgentListener, InboundMessage
from .registry import AgentRegistry


class AgentService:
    """对外稳定的 Agent 合约。

    ``registry`` 管理可见的 Agent 身份，``_driver`` 只在启动组合完成后注入。
    因此 Service 可以先被路由注册，再由 AgentLoop Provider 完成数据面绑定；
    未绑定时查询方法仍可安全返回 idle，但执行方法会明确报“未就绪”。
    """

    key = "agents"

    def __init__(self) -> None:
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

    def attach_driver(self, driver: AgentDriver) -> None:
        """Attach an explicit data-plane port after Provider composition."""
        if not isinstance(driver, AgentDriver):
            raise TypeError("driver must implement AgentDriver")
        if self._driver is not None and self._driver is not driver:
            raise RuntimeError("AgentService already has an attached driver")
        self._driver = driver
        if self.registry.get("default") is None:
            self.registry.register("default", state="ready")

    def detach_driver(self) -> None:
        """Detach the provider during Gateway shutdown; safe to repeat."""
        self._driver = None
        for record in tuple(self.registry.list()):
            self.registry.dispose(record["id"])

    async def run(self, message: InboundMessage) -> Any:
        """执行一条已经由上游交付的 InboundMessage。

        Inbox Package 负责 admission 和 worker；AgentService 只接收这一条
        已交付输入。直接调用时若同一 Session 正在运行，由 Driver 返回 busy 错误，
        而不是在这里隐式创建第二个队列。
        """
        return await self._await(self.driver.run(message))

    async def cancel(self, *args: Any, **kwargs: Any) -> Any:
        """请求 Driver 取消会话中的 active Turn；未交付输入由 InboxService 负责。"""
        return await self._await(self.driver.cancel(*args, **kwargs))

    def status(self, session_id: str) -> str:
        """查询 Session 当前状态；Driver 未绑定时返回 idle。"""
        if self._driver is None:
            return "idle"
        return self._driver.get_session_status(session_id)

    def is_busy(self, session_id: str) -> bool:
        """判断 Session 是否仍有活动 Turn 或维护任务。"""
        return self.status(session_id) in {"running", "processing", "compacting"}

    def get_session_status(self, session_id: str) -> str:
        """兼容公开 AgentDriver 的状态查询命名。"""
        return self.status(session_id)

    def is_session_busy(self, session_id: str) -> bool:
        """兼容公开 AgentDriver 的忙碌查询命名。"""
        return self.is_busy(session_id)

    async def delete_session(self, session_id: str) -> Any:
        """请求 Driver 关闭并删除一个 Session。"""
        return await self._await(self.driver.delete_session(session_id))

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
