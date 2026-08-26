"""Agent 公共 Service：身份注册 + 显式数据面 Runtime。

这里是 HTTP、WebSocket、Inbox 和 Feature 看到的 Agent 边界。它只保存 Agent
的公开身份/状态，并把执行请求交给已绑定的 Runtime；真正的 AgentLoop 由
``ftre-agent-runtime`` 的 Provider Plugin 在 Composition 阶段组装，业务调用方
反向依赖不到 Loop 的私有实现。

Runtime 绑定约定（唯一调用契约，无独立 Port 类型）：被绑定对象实现
``run_inbound`` / ``cancel_session`` / ``get_session_status`` /
``is_active_session`` / ``delete_session`` / ``resume_confirmation``。
PRD-F33 删除了旧的 Driver 适配层过渡接线；没有第二个
Runtime 实现之前不引入 Protocol 层。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .contracts import AgentListener, AgentRunResult, InboundMessage
from .registry import AgentRegistry, HookScopeCarrier


class AgentService:
    """对外稳定的 Agent 合约。

    ``registry`` 管理可见的 Agent 身份，``_runtime`` 只在 Runtime Provider
    完成组装后绑定。因此 Service 可以先被路由注册，再由 Runtime Plugin 完成
    数据面绑定；未绑定时查询方法仍可安全返回 idle，但执行方法会明确报
    "未就绪"。
    """

    key = "agents"

    def __init__(self) -> None:
        self._runtime: Any = None
        self.registry = AgentRegistry()
        self._listeners: dict[str, list[AgentListener]] = {
            "created": [],
            "disposed": [],
        }

    @property
    def runtime(self) -> Any:
        """Return the attached runtime instance, never a wrapper port."""
        if self._runtime is None:
            raise RuntimeError("AgentService runtime is not ready")
        return self._runtime

    def attach_runtime(self, runtime) -> None:
        """Bind the concrete runtime after Provider composition."""
        if self._runtime is not None and self._runtime is not runtime:
            raise RuntimeError("AgentService already has an attached runtime")
        self._runtime = runtime
        if self.registry.get("default") is None:
            self.registry.register("default", state="ready")

    def detach_runtime(self) -> None:
        """Detach the runtime during Gateway shutdown; safe to repeat."""
        self._runtime = None
        for record in tuple(self.registry.list()):
            self.registry.dispose(record["id"])

    async def run(self, message: InboundMessage) -> AgentRunResult:
        """执行一条已经由上游交付的 InboundMessage。

        Inbox Package 负责 admission 和 worker；AgentService 只接收这一条
        已交付输入。直接调用时若同一 Session 正在运行，由 Runtime 返回 busy
        错误，而不是在这里隐式创建第二个队列。
        """
        return await self._await(self.runtime.run_inbound(message))

    async def cancel(self, *args: Any, **kwargs: Any) -> Any:
        """请求 Runtime 取消会话中的 active Turn；未交付输入由 InboxService 负责。"""
        return await self._await(self.runtime.cancel_session(*args, **kwargs))

    def status(self, session_id: str) -> str:
        """查询 Session 当前状态；Runtime 未绑定时返回 idle。"""
        if self._runtime is None:
            return "idle"
        return self._runtime.get_session_status(session_id)

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
        return await self._await(self.runtime.delete_session(session_id))

    async def resume_confirmation(
        self,
        session_id: str,
        channel_id: str,
        events: list[Any],
        metadata: Any,
    ) -> Any:
        """Apply existing confirmation events and resume the paused Agent turn."""
        return await self._await(
            self.runtime.resume_confirmation(
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
