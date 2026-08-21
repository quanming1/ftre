"""
AgentLoop - 全局单例，消费所有 session 的 inbound 消息

职责：
- 从 Bus 全局 inbound 队列消费消息
- 对不同 session 并发执行；同一 session 由唯一 mailbox worker 串行消费
- 系统级指令走高优先级 control lane，不进入普通 FIFO
- Agent 执行全在主事件循环，Task.cancel() 在 LLM stream 的 await 处立即生效

Turn 执行逻辑（状态机驱动）已拆到 TurnExecutor，
AgentLoop 只管消费循环 + 并发控制 + 生命周期。
"""

import asyncio
import inspect
import logging

from cordis import Context
from ftre_agent_core import Tracer
from ftre_agent_core.tool import ToolRegistry

from ftre.platform.hooks import HookRuntime, HookSpec
from ftre.services.agent.config import AgentConfig
from ftre.services.agent.hooks import (
    AGENT_CREATED_SPEC,
    AGENT_DISPOSED_SPEC,
    AgentLifecyclePayload,
    AgentSubject,
)
from ftre.services.agent.registry import AgentRegistry
from ftre.services.compaction import CompactionPort, NullCompactionService
from ftre.services.messaging.bus import (
    BusMessage,
    CommandMessagePayload,
    EventBus,
    InboundMetadata,
    MailboxItemPayload,
    MailboxPhase,
    SessionCommandMessage,
    SessionMailboxSnapshotMessage,
    SessionMailboxSnapshotPayload,
)
from ftre.services.observability.trace.store import TRACE_DB_PATH, SQLiteTraceExporter
from ftre.services.session import SessionService
from ftre.services.session.hooks import (
    SESSION_EVENT_SPEC,
    SESSION_FLUSH_SPEC,
    SessionEventPayload,
    SessionFlushPayload,
)
from ftre.services.session.projection import ProjectionResult, SessionProjection

from ..mailbox.lane import AdmissionResult, SessionLaneRegistry
from ..mailbox.store import MailboxStore
from .completion_registry import CompletionRegistry
from .context_gate import ContextGate
from .turn_executor import TurnExecutor

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    全局单例，消费所有 session 的消息。

    并发模型：
    - _consume：把 inbound 耐久接纳进目标 session 的 Mailbox
    - SessionLane：每个 session 唯一 worker，负责 FIFO/压缩 barrier/状态
    - TurnExecutor.execute：只执行一个已经 claim 的 Turn
    - 所有 Agent 执行在主事件循环，Task.cancel() 可在 LLM stream 的 await 处立即生效

    生命周期：
    - start()  → 启动消费协程
    - stop()   → 关闭接纳入口 + 中断各 Lane + 等待真实压缩任务结束
    """

    def __init__(
        self,
        bus: EventBus,
        session_manager: SessionService,
        channel_manager=None,
        config: AgentConfig = None,
        event_hub=None,
        tool_registry: ToolRegistry | None = None,
        command_service=None,
        plugin_manager=None,
        agent_manager=None,
        agent_registry: AgentRegistry | None = None,
        agent_service=None,
        attachments=None,
        system_prompt=None,
        hook_runtime: HookRuntime | None = None,
        compaction: CompactionPort | None = None,
    ):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.event_hub = event_hub
        self.tool_registry = tool_registry
        self.commands = command_service
        self.plugin_manager = plugin_manager
        self.agent_manager = agent_manager
        self.agent_service = agent_service
        self.attachments = attachments
        self.agent_registry = agent_registry or AgentRegistry()
        self.compaction = compaction or NullCompactionService()
        self.system_prompt = system_prompt
        self._flush_unbind = None
        self._agent_created_emitted: set[str] = set()
        self.hooks = hook_runtime or (
            HookRuntime(event_hub) if isinstance(event_hub, Context) else None
        )
        self._injected_config = config
        self._task: asyncio.Task | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self.tracer = Tracer([SQLiteTraceExporter(TRACE_DB_PATH)])

        # ─── Turn 执行器 ──────────────────────────────────────
        self._executor = TurnExecutor(
            self,
            sessions=session_manager,
            agents=agent_service,
            attachments=attachments,
            system_prompt=system_prompt,
            hooks=self.hooks,
            agent_registry=self.agent_registry,
        )

        # ─── 进行中 Reply 快照注册表 ──────────────────────────
        self.session_projection = SessionProjection(session_manager)
        bind_flush = getattr(session_manager, "bind_flush_dispatcher", None)
        if callable(bind_flush):
            self._flush_unbind = bind_flush(self._flush_session_hooks)

        # SessionLane 的协作对象各只做一件事：持久化、上下文门控、完成等待、串行调度。
        self.mailbox_store = MailboxStore(
            self.session_manager,
            capacity=self._initial_context_cfg().mailbox_capacity,
        )
        self.completions = CompletionRegistry()
        self.context_gate = ContextGate(self.compaction)
        self.lanes = SessionLaneRegistry(
            mailbox=self.mailbox_store,
            context_gate=self.context_gate,
            executor=self._executor,
            completion=self.completions,
            publish_snapshot=self.publish_mailbox_snapshot,
            publish_command_result=self.publish_command_result,
            emit_session_event=self.emit_session_event,
            hooks=self.hooks,
            agent_registry=self.agent_registry,
        )
        # 依赖关系固定为：Lane 编排 → Mailbox 持久化、ContextGate 门控、
        # TurnExecutor 执行、CompletionRegistry 精确等待；这些组件不反向持有 AgentLoop。

    def agent_subject(self, agent_id: str) -> AgentSubject:
        """Resolve a stable Agent identity for Hook scope dispatch."""
        record = self.agent_registry.ensure(agent_id)
        return AgentSubject(agent_id=record.agent_id, identity=record.identity)

    async def dispatch_agent_hook(self, spec: HookSpec, payload, *, agent_id: str):
        """Dispatch an Agent Hook through the official Cordis scope carrier."""
        hooks = self.hooks
        if hooks is None:
            if spec.default is None:
                return None
            result = spec.default(payload)
            return await result if inspect.isawaitable(result) else result
        registry = self.agent_registry
        created_emitted = self._agent_created_emitted
        if spec.name != AGENT_CREATED_SPEC.name and agent_id not in created_emitted:
            created_emitted.add(agent_id)
            record = registry.ensure(agent_id)
            created_payload = AgentLifecyclePayload(
                agent=AgentSubject(agent_id, record.identity),
                state="created",
            )
            await self._dispatch_agent_hook(
                AGENT_CREATED_SPEC,
                created_payload,
                agent_id=agent_id,
            )
        return await self._dispatch_agent_hook(spec, payload, agent_id=agent_id)

    async def _dispatch_agent_hook(self, spec: HookSpec, payload, *, agent_id: str):
        """Dispatch one already-scoped Hook without lifecycle side effects."""
        hooks = self.hooks
        if hooks is None:
            if spec.default is None:
                return None
            result = spec.default(payload)
            return await result if inspect.isawaitable(result) else result
        registry = self.agent_registry
        registry.ensure(agent_id)
        carrier = registry.scope_carrier(agent_id)
        scope_context = hooks.context_for_scope(carrier)
        return await hooks.dispatch(spec, payload, context=scope_context)

    def _initial_context_cfg(self):
        """实例化时读一次 ContextConfig 用于默认上下文门控参数。"""
        try:
            cfg = self._load_current_config()
            return cfg.context
        except Exception:
            logger.debug("[agent-loop] 使用默认 ContextConfig", exc_info=True)
            from ftre.services.agent.config import ContextConfig

            return ContextConfig()

    def start(self) -> None:
        """启动消费循环"""
        if self._task is not None and not self._task.done():
            return
        self._event_loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._consume())

    def is_session_running(self, session_id: str) -> bool:
        """该 session 是否有正在跑的 ReActAgent。"""
        return self.lanes.operation_for(session_id) is not None

    def is_session_busy(self, session_id: str) -> bool:
        """是否有 Turn、压缩或尚未消费的请求。"""
        if self.lanes.operation_for(session_id) is not None:
            return True
        return self.session_manager.has_mailbox_work(session_id)

    def get_session_status(self, session_id: str) -> str:
        """返回 SessionLane 推导出的公开 activity。"""
        operation = self.lanes.operation_for(session_id)
        if operation is not None:
            if type(operation).__name__ == "TurnOperation":
                return operation.state
            return {
                "TurnOperation": "running",
                "CompactOperation": "compacting",
                "BlockedOperation": "blocked",
            }.get(type(operation).__name__, "running")
        # pending 是 mailbox 内容，不是第二种运行态；没有 active 时就是 idle。
        return "idle"

    async def cancel_session(
        self, session_id: str, *, expected_request_id: str = ""
    ) -> bool:
        """取消指定 session 正在运行的 Agent 与 turn task（与 /cancel 同逻辑）。

        必须在事件循环线程调用（cancel_nowait 触达 asyncio.Task）。
        """
        return await self.lanes.cancel_active(session_id, expected_request_id)

    async def set_session_compacting(self, session_id: str, value: bool) -> bool:
        """仅供当前 Turn 内的手工压缩标注状态，不改变 Lane 的执行所有权。"""
        return await self.lanes.set_turn_compacting(session_id, value)

    async def delete_session(self, session_id: str) -> None:
        """删除编排归 AgentLoop 所有：先关闭 Lane，再让 SessionManager 删除持久数据。"""
        meta = await self.session_manager.get_session_metadata(session_id)
        member_ids: list[str] = []
        teams = meta.get("teams") if isinstance(meta, dict) else None
        if isinstance(teams, dict):
            for team in teams.values():
                if isinstance(team, dict) and isinstance(team.get("members"), dict):
                    member_ids.extend(str(sid) for sid in team["members"])
        # 先一次性竖起 leader/成员的关闭栅栏，避免并发提交在逐个 close 的间隙
        # 重新创建 Lane；栅栏之后才删除各自 state.json。
        await self.lanes.close_sessions((session_id, *member_ids))
        await self.session_manager.delete_session(session_id)

    async def submit_inbound(self, inbound: BusMessage) -> AdmissionResult:
        """供 send_message 等可信内部生产者直接调用的可靠入队接口。"""
        # 内部工具也必须走 Bus request/reply，不能直接调用 Lane，否则会绕过统一路由与停止栅栏。
        return await self.bus.request_inbound(inbound)

    async def wait_request(self, session_id: str, request_id: str):
        """等待某一个精确 request 的持久化终态。"""
        return await self.completions.wait(session_id, request_id)

    async def wait_session_quiescent(self, session_id: str) -> dict:
        """等待当前 Turn、压缩任务和已接纳 FIFO 全部清空。"""
        while self.is_session_busy(session_id):
            await asyncio.sleep(0.02)
        return {"session_id": session_id, "status": "quiescent"}

    async def cancel_queued_message(self, session_id: str, request_id: str):
        """取消一条尚未开始执行的队列消息。"""
        return await self.lanes.cancel_pending(session_id, request_id)

    async def stop(self) -> None:
        """优雅关闭：取消消费循环 + 中断所有 Agent。"""
        # 先关闭 Bus admission，避免消费协程停止后入口仍在等待没有人处理的请求。
        self.bus.stop_inbound()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

        await self.lanes.stop()
        # 覆盖手工 /compact 等不归某个 Lane operation 所有的共享压缩任务。
        await self.compaction.cancel_all_compact_tasks()
        await self._dispose_agent_scopes()
        if self._flush_unbind is not None:
            self._flush_unbind()
            self._flush_unbind = None

    async def _dispose_agent_scopes(self) -> None:
        """Close Agent scope observations before the Provider detaches its driver."""
        hooks = self.hooks
        if hooks is None:
            return
        registry = self.agent_registry
        for record in tuple(registry.list()):
            agent_id = record["id"]
            current = registry.ensure(agent_id)
            payload = AgentLifecyclePayload(
                agent=AgentSubject(agent_id, current.identity),
                state="disposed",
            )
            try:
                await self._dispatch_agent_hook(
                    AGENT_DISPOSED_SPEC,
                    payload,
                    agent_id=agent_id,
                )
            except Exception:
                logger.exception("[agent-loop] agent/disposed failed agent=%s", agent_id)
            registry.dispose(agent_id)

    # ─── 消费循环 ────────────────────────────────────────────

    async def _consume(self) -> None:
        """消费循环：按 Bus 到达顺序把消息接纳进每个 session 的 Mailbox。"""
        # 这里不执行 Agent；这里只负责“全局消息 → 对应 Lane 的 durable admission”。
        # 真正的 FIFO 和并发边界由每个 SessionLane 自己维护。
        try:
            await self._emit_existing_agent_created()
            # 恢复持久化 Mailbox（并安全终止上次进程遗留的 active 请求）后才
            # 开放新消息消费；恢复期间 EventBus 会继续保留到达消息。
            await self.lanes.recover()
            async for msg in self.bus.subscribe_inbound():
                try:
                    # 取消是独立的控制 BusMessage，不伪装成一条 /cancel 用户消息，
                    # 因而既不会写入 messages，也不会占用 mailbox 队列。
                    if msg.type == "turn_cancel":
                        result = AdmissionResult(
                            accepted=True,
                            session_id=str(
                                msg.data.get("session_id") or msg.from_session
                            ),
                            created=await self.lanes.cancel_active(
                                str(msg.data.get("session_id") or msg.from_session),
                                str(msg.data.get("expected_request_id") or ""),
                            ),
                        )
                    else:
                        command = self._parse_ingress_command(msg)
                        if command is not None and command.system:
                            await self._dispatch_system_command(msg)
                            result = AdmissionResult(
                                accepted=True,
                                session_id=str(
                                    msg.data.get("session_id") or msg.from_session
                                ),
                                request_id=msg.metadata.request_id,
                                created=True,
                            )
                        elif command is not None:
                            result = await self.lanes.dispatch_command(
                                msg, command, self.commands
                            )
                        else:
                            result = await self.lanes.submit(msg)
                    self.bus.resolve_inbound(msg.id, result)
                except Exception:
                    logger.exception("[agent-loop] inbound 接纳失败 message=%s", msg.id)
                    self.bus.reject_inbound(msg.id, RuntimeError("AgentLoop 接纳失败"))
        except asyncio.CancelledError:
            pass

    def _parse_ingress_command(self, msg: BusMessage):
        """Parse commands before they enter Mailbox/Turn execution."""
        if self.commands is None:
            return None
        return self.commands.parse({"inbound": msg})

    async def _dispatch_system_command(self, msg: BusMessage) -> bool:
        """Run a system command on the control lane without mailbox admission."""
        if self.commands is None:
            return False
        return (await self.commands.dispatch_inbound(msg, system=True)) is not None

    async def _emit_existing_agent_created(self) -> None:
        """Publish created once for identities supplied by AgentService at startup."""
        hooks = self.hooks
        registry = self.agent_registry
        if hooks is None or registry is None:
            return
        emitted = self._agent_created_emitted
        for record in tuple(registry.list()):
            if record["id"] in emitted:
                continue
            agent_id = record["id"]
            current = registry.ensure(agent_id)
            try:
                await self._dispatch_agent_hook(
                    AGENT_CREATED_SPEC,
                    AgentLifecyclePayload(
                        agent=AgentSubject(agent_id, current.identity),
                        state="created",
                    ),
                    agent_id=agent_id,
                )
            except Exception:
                logger.exception("[agent-loop] agent/created failed agent=%s", agent_id)
            emitted.add(agent_id)

    def _load_current_config(self) -> AgentConfig:
        """读取当前生效的配置（委托给 TurnExecutor）。"""
        return self._executor._load_current_config()

    async def emit_session_event(
        self,
        session_id: str,
        channel_id: str,
        event,
        *,
        metadata: InboundMetadata | None = None,
    ) -> "ProjectionResult":
        """统一事件出口：先投影落盘，再实时广播。

        Compaction Service 与 TurnExecutor 共用此入口，保证"Projection 落盘成功 →
        广播 WebSocket"的顺序。dispatch 序列化的是 core Event 本身，不嵌套私有
        {type, data} 协议。
        """

        result = await self.session_projection.apply(session_id, event)
        await self._emit_session_event_hook(session_id, event, result)
        await self.bus.publish_outbound(
            BusMessage(
                type="agent_event",
                from_channel=channel_id,
                to_channel=channel_id,
                from_session=session_id,
                to_session=session_id,
                data=event.model_dump(mode="json"),
                metadata=metadata or InboundMetadata(),
            )
        )
        return result

    async def publish_command_result(self, inbound: BusMessage, result) -> None:
        """Publish a direct Command result without opening an Agent Turn."""
        if result is None or not getattr(result, "text", ""):
            return
        level = "error" if getattr(result, "kind", "success") == "error" else "info"
        await self.bus.publish_outbound(
            SessionCommandMessage(
                from_channel=inbound.from_channel,
                to_channel=inbound.from_channel,
                from_session=inbound.from_session,
                to_session=inbound.from_session,
                data=CommandMessagePayload(content=result.text, level=level),
                metadata=inbound.metadata,
            )
        )

    async def resume_confirmation(
        self,
        session_id: str,
        channel_id: str,
        events: list,
        metadata: InboundMetadata,
    ):
        """Resume a paused Agent through the existing Session Event path."""
        inbound = BusMessage(
            type="user_message",
            from_channel=channel_id,
            from_session=session_id,
            to_channel="agent",
            to_session=session_id,
            data={"session_id": session_id, "content": ""},
            metadata=metadata,
        )
        return await self.lanes.resume_confirmation(inbound, events)

    async def _emit_session_event_hook(self, session_id: str, event, result) -> None:
        """Notify observers only after SessionProjection has committed the fact."""
        hooks = self.hooks
        if hooks is None:
            return
        payload = SessionEventPayload(
            session_id=session_id,
            event=event,
            persisted_ids=tuple(message.id for message in result.persisted_messages),
            completed_id=(result.completed_message.id if result.completed_message else ""),
        )
        try:
            await hooks.dispatch(SESSION_EVENT_SPEC, payload)
        except Exception:
            logger.exception(
                "[agent-loop] session/event observer failed session=%s", session_id
            )

    async def _flush_session_hooks(self, session_id: str, reason: str) -> None:
        hooks = self.hooks
        if hooks is None:
            return
        await hooks.dispatch(
            SESSION_FLUSH_SPEC,
            SessionFlushPayload(session_id, reason, asyncio.Event()),
        )

    async def _publish_session_status_async(self, session_id: str, status: str) -> None:
        """命令处理器只触发刷新；状态始终由 SessionLane 的 operation 推导。"""
        del status
        await self.publish_mailbox_snapshot(session_id)

    async def _build_mailbox_snapshot(
        self, session_id: str
    ) -> tuple[str, SessionMailboxSnapshotPayload]:
        """在一个地方把持久 Mailbox 与瞬时 Lane operation 合成为对外快照。"""
        # Mailbox 只提供 pending；Lane operation 是运行中状态的唯一内存事实源。
        # 两者合成后才是客户端可用的完整状态，避免客户端自己猜测后台阶段。
        mailbox = await self.lanes.snapshot(session_id)
        operation = self.lanes.operation_for(session_id)
        phase = MailboxPhase.IDLE
        blocked_reason = None
        can_cancel_active = False
        if operation is not None:
            name = type(operation).__name__
            if name == "TurnOperation":
                phase = MailboxPhase(operation.state)
                can_cancel_active = operation.state == "running"
            elif name == "CompactOperation":
                phase = MailboxPhase.COMPACTING
            elif name == "BlockedOperation":
                phase = MailboxPhase.BLOCKED
                blocked_reason = operation.reason
        session = await self.session_manager.get_session(session_id)
        channel_id = session["channel_id"] if session else ""
        pending = [
            MailboxItemPayload(
                request_id=item.request_id,
                # 当前桌面端尚未迁移 request_id 字段，这里在 WS 只做同值别名。
                # 存储和执行逻辑均只有 request_id 一个业务标识。
                sequence=item.sequence,
                content=item.content,
                attachments=item.attachments,
                source="user",
            )
            for item in mailbox.pending
        ]
        return channel_id, SessionMailboxSnapshotPayload(
            session_id=session_id,
            revision=mailbox.revision,
            phase=phase,
            pending=pending,
            capacity=self.mailbox_store.capacity,
            accepting_messages=phase
            not in {MailboxPhase.COMPACTING, MailboxPhase.BLOCKED},
            can_cancel_active=can_cancel_active,
            blocked_reason=blocked_reason,
        )

    async def publish_mailbox_snapshot(self, session_id: str) -> None:
        """发布由 SessionLane 唯一所有的 mailbox 完整快照。"""
        channel_id, payload = await self._build_mailbox_snapshot(session_id)
        await self.bus.publish_outbound(
            SessionMailboxSnapshotMessage(
                from_channel=channel_id,
                to_channel=channel_id,
                from_session=session_id,
                to_session=session_id,
                data=payload,
            )
        )

    async def get_mailbox_snapshot(self, session_id: str) -> dict:
        """给 HTTP/WS attach 的窄只读查询口；外部无需知道 SessionLane 实例。"""
        _channel_id, payload = await self._build_mailbox_snapshot(session_id)
        return payload.model_dump(mode="json")
