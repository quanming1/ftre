"""Agent 公共 Service：身份、状态和 Runtime Factory 调度。

``ftre-agent`` 是公开的 Service Owner。具体 AgentLoop 由 Runtime Provider
注册到这里，但不会反过来创建或发布 ``agents`` Service。这样 Gateway、Inbox、
HTTP 和 Channel 只依赖本模块，不会看到 Runtime 的具体实现。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .contracts import (
    AgentCreateSpec,
    AgentListener,
    AgentResumeSpec,
    AgentRunRequest,
    AgentRunResult,
    AgentRuntimeFactory,
    AgentView,
    RunReservation,
)
from .errors import (
    AgentAlreadyExistsError,
    AgentBusyError,
    AgentNotFoundError,
    FactoryAlreadyRegisteredError,
    FactoryNotRegisteredError,
    FactoryRegistrationMismatchError,
    InvalidFactoryError,
    InvalidRunRequestError,
    ServiceClosedError,
)
from .registry import AgentRegistry, HookScopeCarrier

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FactoryRegistration:
    """一次 Runtime Factory 注册的不可变句柄。"""

    token: object
    name: str


def _factory_name(factory: Any) -> str:
    return str(getattr(factory, "name", None) or factory.__class__.__name__)


@dataclass(slots=True)
class _AgentEntry:
    spec: AgentCreateSpec | AgentResumeSpec
    runtime: Any
    state: str = "idle"
    run_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AgentHandle:
    """AgentService 返回的操作句柄；不持有第二份运行状态。"""

    def __init__(self, service: AgentService, agent_id: str) -> None:
        self._service = service
        self._agent_id = agent_id

    @property
    def id(self) -> str:
        return self._agent_id

    def view(self) -> AgentView:
        return self._service.view(self._agent_id)

    def status(self) -> str:
        return self._service.status(self._agent_id)

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        return await self._service.run(self._agent_id, request)

    async def stream(self, request: AgentRunRequest):
        async for event in self._service.stream(self._agent_id, request):
            yield event

    async def cancel(self, reason: str = "") -> Any:
        return await self._service.cancel(self._agent_id, reason=reason)

    async def dispose(self) -> None:
        await self._service.dispose(self._agent_id)


class AgentService:
    """对外稳定的 Agent 合约和唯一 ``agents`` Service Owner。"""

    key = "agents"

    def __init__(self) -> None:
        self._factory: Any = None
        self._factory_registration: FactoryRegistration | None = None
        self._closed = False
        self.registry = AgentRegistry()
        self._entries: dict[str, _AgentEntry] = {}
        self._reservations: dict[str, RunReservation] = {}
        self._listeners: dict[str, list[AgentListener]] = {
            "created": [],
            "disposed": [],
            "status-changed": [],
        }

    def start(self) -> None:
        """允许 Composition 在提供 Service 后显式启动它。"""
        self._closed = False

    def close(self) -> None:
        """关闭 Service；Runtime Factory 的生命周期由其 Provider 管理。"""
        self._closed = True
        self._factory = None
        self._factory_registration = None
        self._entries.clear()
        self._reservations.clear()
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
        self._entries.clear()
        self._reservations.clear()
        for record in tuple(self.registry.list()):
            self.registry.dispose(record["id"])
        return True

    @property
    def factory_name(self) -> str | None:
        """只提供诊断名称，不暴露 Runtime 对象。"""
        return self._factory_registration.name if self._factory_registration else None

    def is_ready(self) -> bool:
        return not self._closed and self._factory is not None

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise InvalidRunRequestError("agent_id must be non-empty")

    def _entry_or_raise(self, agent_id: str) -> _AgentEntry:
        entry = self._entries.get(agent_id)
        if entry is None:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return entry

    @staticmethod
    def _state_from_result(result: AgentRunResult) -> str:
        if result.status == "cancelled":
            return "cancelled"
        if result.status == "failed":
            return "failed"
        return "idle"

    @staticmethod
    def _with_run_id(result: AgentRunResult, run_id: str) -> AgentRunResult:
        if result.run_id:
            return result
        return replace(result, run_id=run_id)

    async def _notify(self, event: str, view: AgentView) -> None:
        for callback in tuple(self._listeners.get(event, ())):
            try:
                result = callback(view)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("AgentService observer failed event=%s agent=%s", event, view.agent_id)

    def _factory_or_raise(self) -> Any:
        if self._closed:
            raise ServiceClosedError("AgentService is closed")
        if self._factory is None:
            raise FactoryNotRegisteredError("AgentService Runtime Factory is not ready")
        return self._factory

    async def create(self, spec: AgentCreateSpec) -> AgentHandle:
        """创建一个 Agent identity，并委托 Runtime Factory 建立运行句柄。"""
        factory = self._factory_or_raise()
        self._validate_agent_id(spec.agent_id)
        if spec.agent_id in self._entries:
            raise AgentAlreadyExistsError(f"Agent already exists: {spec.agent_id}")
        create = getattr(factory, "create", None)
        if not callable(create):
            raise InvalidFactoryError("Runtime Factory does not implement create")
        runtime = await self._await(create(spec))
        self.registry.register(spec.agent_id, state="idle")
        self._entries[spec.agent_id] = _AgentEntry(spec=spec, runtime=runtime)
        await self._notify("created", self.view(spec.agent_id))
        return AgentHandle(self, spec.agent_id)

    async def resume(self, spec: AgentResumeSpec) -> AgentHandle:
        """恢复已有 Agent identity；配置校验由 Runtime/Session Service 完成。"""
        factory = self._factory_or_raise()
        self._validate_agent_id(spec.agent_id)
        if spec.agent_id in self._entries:
            raise AgentAlreadyExistsError(f"Agent already exists: {spec.agent_id}")
        resume = getattr(factory, "resume", None)
        if not callable(resume):
            raise InvalidFactoryError("Runtime Factory does not implement resume")
        runtime = await self._await(resume(spec))
        self.registry.register(spec.agent_id, state="idle")
        self._entries[spec.agent_id] = _AgentEntry(spec=spec, runtime=runtime)
        await self._notify("created", self.view(spec.agent_id))
        return AgentHandle(self, spec.agent_id)

    def view(self, agent_id: str) -> AgentView:
        entry = self._entries.get(agent_id)
        if entry is None:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        config_hash = getattr(entry.spec.config, "snapshot_hash", None)
        return AgentView(
            agent_id=agent_id,
            state=entry.state,
            session_id=entry.spec.session_id,
            run_id=entry.run_id,
            created_at=entry.created_at,
            config_snapshot_hash=config_hash,
        )

    async def run(
        self,
        agent_id: str,
        request: AgentRunRequest,
    ) -> AgentRunResult:
        """执行一轮 AgentRun。"""
        self._factory_or_raise()
        if not isinstance(agent_id, str):
            raise InvalidRunRequestError("agent_id must be a string")
        entry = self._entry_or_raise(agent_id)
        self._expire_reservations()
        if entry.state in {"running", "stopping", "compacting"}:
            raise AgentBusyError(f"Agent is busy: {agent_id}")
        if request.session_id != (entry.spec.session_id or agent_id):
            raise InvalidRunRequestError("request session does not belong to Agent")
        entry.state = "running"
        entry.run_id = request.request_id
        self._consume_reservation(
            agent_id,
            request.session_id,
            request.request_id,
        )
        self.registry.set_state(agent_id, "running")
        await self._notify("status-changed", self.view(agent_id))
        try:
            result = await self._await(entry.runtime.run(request))
            result = self._with_run_id(result, request.request_id)
            entry.state = self._state_from_result(result)
            return result
        except asyncio.CancelledError:
            entry.state = "cancelled"
            raise
        finally:
            if entry.state == "running":
                entry.state = "idle"
            self.registry.set_state(agent_id, entry.state)
            await self._notify("status-changed", self.view(agent_id))

    async def stream(self, agent_id: str, request: AgentRunRequest):
        """转发 Runtime 事件流，并在流结束时更新 Agent 状态。"""
        entry = self._entry_or_raise(agent_id)
        if entry.state in {"running", "stopping", "compacting"}:
            raise AgentBusyError(f"Agent is busy: {agent_id}")
        if request.session_id != (entry.spec.session_id or agent_id):
            raise InvalidRunRequestError("request session does not belong to Agent")
        stream = getattr(entry.runtime, "stream", None)
        if not callable(stream):
            raise InvalidFactoryError("Runtime handle does not implement stream")
        entry.state = "running"
        entry.run_id = request.request_id
        self.registry.set_state(agent_id, "running")
        await self._notify("status-changed", self.view(agent_id))
        try:
            async for event in stream(request):
                yield event
            entry.state = "idle"
        finally:
            if entry.state == "running":
                entry.state = "idle"
            self.registry.set_state(agent_id, entry.state)
            await self._notify("status-changed", self.view(agent_id))

    async def cancel(self, agent_id: str, reason: str = "", **kwargs: Any) -> Any:
        """请求 Runtime 取消会话中的 active Turn；未交付输入由 InboxService 负责。"""
        entry = self._entries.get(agent_id)
        if entry is not None:
            entry.state = "stopping"
            self.registry.set_state(agent_id, "stopping")
            cancel = getattr(entry.runtime, "cancel", None)
            if callable(cancel):
                result = await self._await(cancel(reason))
            else:
                result = await self._await(
                    self._factory_or_raise().cancel_session(
                        entry.spec.session_id or agent_id, **kwargs
                    )
                )
            entry.state = "cancelled" if result else "idle"
            self.registry.set_state(agent_id, entry.state)
            await self._notify("status-changed", self.view(agent_id))
            return result
        return await self._await(
            self._factory_or_raise().cancel_session(agent_id, **kwargs)
        )

    def status(self, session_id: str) -> str:
        """查询 Session 当前状态；Runtime 未绑定时返回 idle。"""
        entry = self._entries.get(session_id)
        if entry is not None:
            return entry.state
        if self._factory is None:
            return "idle"
        return self._factory.get_session_status(session_id)

    def is_busy(self, session_id: str) -> bool:
        """判断 Session 是否仍有活动 Turn 或维护任务。"""
        self._expire_reservations()
        return self.status(session_id) in {"running", "processing", "compacting"} or any(
            item.session_id == session_id for item in self._reservations.values()
        )

    def try_reserve(
        self,
        agent_id: str,
        session_id: str,
        request_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> RunReservation | None:
        """原子保留一次 Run；Inbox 在 durable claim 前调用。"""
        self._factory_or_raise()
        self._validate_agent_id(agent_id)
        if not session_id or not request_id:
            raise InvalidRunRequestError("session_id and request_id must be non-empty")
        entry = self._entries.get(agent_id)
        if entry is None:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        self._expire_reservations()
        if entry.state in {"running", "stopping", "compacting"}:
            return None
        if any(item.agent_id == agent_id for item in self._reservations.values()):
            return None
        reservation = RunReservation(
            reservation_id=uuid4().hex,
            agent_id=agent_id,
            session_id=session_id,
            request_id=request_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=max(0.1, lease_seconds)),
        )
        self._reservations[reservation.reservation_id] = reservation
        return reservation

    def release_reservation(self, reservation: RunReservation | str) -> bool:
        """释放一次尚未消费的 Run 保留；重复释放安全。"""
        reservation_id = (
            reservation.reservation_id if isinstance(reservation, RunReservation) else reservation
        )
        return self._reservations.pop(reservation_id, None) is not None

    def _consume_reservation(self, agent_id: str, session_id: str, request_id: str) -> None:
        for reservation_id, reservation in tuple(self._reservations.items()):
            if (
                reservation.agent_id == agent_id
                and reservation.session_id == session_id
                and reservation.request_id == request_id
            ):
                self._reservations.pop(reservation_id, None)
                return

    def _expire_reservations(self) -> None:
        now = datetime.now(UTC)
        for reservation_id, reservation in tuple(self._reservations.items()):
            if reservation.expires_at <= now:
                self._reservations.pop(reservation_id, None)

    def get_session_status(self, session_id: str) -> str:
        """公开状态查询命名，与 Runtime 的 ``get_session_status`` 对齐。"""
        return self.status(session_id)

    def is_session_busy(self, session_id: str) -> bool:
        """公开忙碌查询命名；语义与 ``is_busy`` 一致。"""
        return self.is_busy(session_id)

    async def delete_session(self, session_id: str) -> Any:
        """请求 Runtime 关闭并删除一个 Session。"""
        result = await self._await(self._factory_or_raise().delete_session(session_id))
        for agent_id, entry in tuple(self._entries.items()):
            if entry.spec.session_id == session_id:
                await self.dispose(agent_id)
        return result

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

    async def dispose(self, agent_id: str) -> None:
        """释放一个 Agent Handle 及其 Runtime 句柄。"""
        entry = self._entry_or_raise(agent_id)
        dispose = getattr(entry.runtime, "dispose", None)
        if callable(dispose):
            await self._await(dispose())
        self._entries.pop(agent_id, None)
        self.registry.dispose(agent_id)
        await self._notify("disposed", AgentView(agent_id=agent_id, state="disposed"))

    def list(self) -> tuple[AgentView, ...]:
        """返回只读 Agent 视图，不暴露 Runtime 或 Registry。"""
        return tuple(self.view(agent_id) for agent_id in self._entries)

    def get(self, agent_id: str) -> AgentView | None:
        """读取一个 Agent 的公开状态摘要。"""
        return self.view(agent_id) if agent_id in self._entries else None

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

    def on_status_changed(self, callback: AgentListener) -> Callable[[], bool]:
        """订阅 Agent 状态变化；观察回调失败不影响状态提交。"""
        return self._listen("status-changed", callback)

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


__all__ = ["AgentHandle", "AgentService", "FactoryRegistration"]
