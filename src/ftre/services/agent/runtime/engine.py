"""Agent 的私有 active-Turn Runtime。

MessageBus、Command 和 Inbox 在接入平面完成裁决；这里收到的只有已经交付的
``InboundMessage``。Loop 只负责 active Turn、Hook、Projection 和取消。
"""

import asyncio
import inspect
import logging
import uuid

from cordis import Context
from ftre_agent_core import Tracer
from ftre_agent_core.event import UserMessageEvent
from ftre_agent_core.message import from_openai_message
from ftre_agent_core.tool import ToolRegistry

from ftre.kernel.hooks import HookRuntime, HookSpec
from ftre.services.agent.config import AgentConfig
from ftre.services.agent.contracts import InboundMessage
from ftre.services.agent.hooks import (
    AGENT_AFTER_RUN_SPEC,
    AGENT_BEFORE_RUN_SPEC,
    AfterRunPayload,
    AgentSubject,
    BeforeRunPayload,
    RejectRun,
)
from ftre.services.agent.registry import AgentRegistry
from ftre.services.messaging.bus import (
    BusMessage,
    EventBus,
    InboundMetadata,
)
from ftre.services.session import SessionService
from ftre.services.session.message.multimodal import (
    build_user_content,
    normalize_stored_user_content,
)

from .completion import CompletionRegistry
from .turn_executor import TurnExecutor, TurnOutcome

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    全进程共享的 active Turn Runtime。

    并发模型：
    - run_inbound：只执行一个已由 Messaging/Inbox 交付的 InboundMessage
    - 所有 Agent 执行在主事件循环，Task.cancel() 可在 LLM stream 的 await 处立即生效

    生命周期：
    - start()  → 发布已有 Agent identity 的 lifecycle 观察事件
    - stop()   → 中断 active Turn + 等待 Hook 维护结束
    """

    def __init__(
        self,
        bus: EventBus,
        session_manager: SessionService,
        channel_manager=None,
        config: AgentConfig = None,
        event_hub=None,
        tool_registry: ToolRegistry | None = None,
        tool_service=None,
        mcp_service=None,
        plugin_manager=None,
        agent_manager=None,
        agent_registry: AgentRegistry | None = None,
        agent_service=None,
        attachments=None,
        system_prompt=None,
        hook_runtime: HookRuntime | None = None,
        traces=None,
        session_events=None,
    ):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.event_hub = event_hub
        self.tool_registry = tool_registry
        self.tool_service = tool_service
        self.mcp_service = mcp_service
        self.plugin_manager = plugin_manager
        self.agent_manager = agent_manager
        self.agent_service = agent_service
        self.attachments = attachments
        self.agent_registry = agent_registry or AgentRegistry()
        self.system_prompt = system_prompt
        self.session_events = session_events
        self.hooks = hook_runtime or (
            HookRuntime(event_hub) if isinstance(event_hub, Context) else None
        )
        self._injected_config = config
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # 直接 AgentService.run 的 active guard。它只记录运行中的 Turn，绝不保存
        # pending；队列由可选 ftre-inbox Package 独立拥有。
        self._direct_tasks: dict[str, asyncio.Task] = {}
        self._direct_signals: dict[str, asyncio.Event] = {}
        # run_inbound 的父协程还要执行 after-turn/Inbox 唤醒；删除必须等这段
        # 收尾也完成，不能只等待 TurnExecutor 子任务。
        self._direct_completion_events: dict[str, asyncio.Event] = {}
        self._direct_parent_tasks: dict[str, asyncio.Task | None] = {}
        self._direct_reservations: set[str] = set()
        build_tracer = getattr(traces, "build_tracer", None)
        self.tracer = build_tracer() if callable(build_tracer) else Tracer([])

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
        # SessionService is the sole projection owner. The Agent runtime only
        # consumes its public projection capability and never constructs a peer.
        self.session_projection = session_manager.projection

        self.completions = CompletionRegistry()

    def agent_subject(self, agent_id: str) -> AgentSubject:
        """Resolve a stable Agent identity for Hook scope dispatch."""
        record = self.agent_registry.ensure(agent_id)
        return AgentSubject(agent_id=record.agent_id, identity=record.identity)

    async def tool_registry_for_agent(self, agent_id: str, profile=None):
        """Build the isolated tool registry consumed by one ReActAgent turn."""
        mcp_service = self.mcp_service
        tool_service = self.tool_service
        if mcp_service is not None and profile is not None:
            await mcp_service.prepare_agent(agent_id, profile.mcp_config)
        if tool_service is not None:
            return tool_service.build_view(agent_id)
        return self.tool_registry

    async def dispatch_agent_hook(self, spec: HookSpec, payload, *, agent_id: str):
        """Dispatch an Agent Hook through the official Cordis scope carrier."""
        hooks = self.hooks
        if hooks is None:
            if spec.default is None:
                return None
            result = spec.default(payload)
            return await result if inspect.isawaitable(result) else result
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

    def start(self) -> None:
        """Start the active Run runtime without consuming the MessageBus."""
        self._event_loop = asyncio.get_running_loop()

    def is_session_running(self, session_id: str) -> bool:
        """该 session 是否有正在跑的 ReActAgent。"""
        return session_id in self._direct_tasks or session_id in self._direct_reservations

    def is_active_session(self, session_id: str) -> bool:
        """只判断 AgentService active Turn，不读取任何 pending 队列。"""
        return self.is_session_running(session_id) or session_id in self._direct_tasks

    def get_session_status(self, session_id: str) -> str:
        """返回 Agent active Turn 的公开 activity；不读取 Inbox pending。"""
        if session_id in self._direct_tasks:
            return "running"
        return "idle"

    async def cancel_session(
        self, session_id: str, *, expected_request_id: str = ""
    ) -> bool:
        """取消指定 session 正在运行的 Agent 与 turn task（与 /cancel 同逻辑）。

        必须在事件循环线程调用（cancel_nowait 触达 asyncio.Task）。
        """
        if session_id in self._direct_tasks:
            signal = self._direct_signals.get(session_id)
            if signal is not None:
                signal.set()
            task = self._direct_tasks.get(session_id)
            if task is not None and not task.done():
                task.cancel()
            return True
        return False

    async def _cancel_session_and_wait(self, session_id: str) -> bool:
        """取消 active Turn 并等待其所有收尾持久化完成。

        普通 ``session.cancel`` 需要快速返回 ACK，因此 ``cancel_session`` 保持
        "发出取消即返回" 的协议语义。删除 Session 则不同：如果此时直接删掉
        state.json，正在收尾的 Reply 仍可能执行 ``update_message``，最终把已
        完成的助手消息变成 ``message 不存在``。删除路径必须使用这个等待版本，
        确认 Turn 的 ``PIPELINE_END``、Hook 和消息投影都结束后再删除历史。
        """
        task = self._direct_tasks.get(session_id)
        if task is None:
            return False

        signal = self._direct_signals.get(session_id)
        if signal is not None:
            signal.set()
        if task is asyncio.current_task():
            # 防止未来某个内部调用从 active Turn 自身触发删除时自等待死锁。
            return True
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        completion = getattr(self, "_direct_completion_events", {}).get(session_id)
        parent = getattr(self, "_direct_parent_tasks", {}).get(session_id)
        if completion is not None and parent is not asyncio.current_task():
            await completion.wait()
        return True

    async def run_inbound(self, message: InboundMessage) -> TurnOutcome:
        """执行一个已由 Inbox Package 交付的输入。

        这个入口只执行已交付的 ``InboundMessage``；Inbox Package 决定消息何时到达这里。
        """
        session_id = message.session_id
        if self.is_active_session(session_id):
            return TurnOutcome(
                turn_id="",
                status="error",
                error={
                    "code": "agent-busy",
                    "message": "Session 当前已有 active Turn",
                    "retryable": True,
                },
            )
        if self._event_loop is None:
            self._event_loop = asyncio.get_running_loop()
        self._direct_reservations.add(session_id)
        metadata_values = dict(message.metadata or {})
        metadata_values["request_id"] = message.request_id
        metadata = InboundMetadata.model_validate(metadata_values)
        inbound = BusMessage(
            type="user_message",
            from_channel=message.channel_id,
            from_session=session_id,
            to_channel="agent",
            to_session=session_id,
            data={
                "session_id": session_id,
                "content": message.content,
                "attachments": [dict(item) for item in message.attachments],
            },
            metadata=metadata,
        )
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        cancellation = asyncio.Event()
        executed = False
        completion = asyncio.Event()
        self._direct_completion_events[session_id] = completion
        self._direct_parent_tasks[session_id] = asyncio.current_task()
        try:
            validation_error = await self._validate_inbound(message)
            if validation_error is not None:
                return TurnOutcome(
                    turn_id=turn_id,
                    status="error",
                    error=validation_error,
                )
            config, profile = await self._executor.resolve_inbound_config(inbound, turn_id=turn_id)
            agent_id = metadata.agent_id or "default"
            current = self.agent_registry.ensure(agent_id)
            step_decision = await self._dispatch_agent_hook(
                AGENT_BEFORE_RUN_SPEC,
                BeforeRunPayload(
                    agent=AgentSubject(agent_id, current.identity),
                    session_id=session_id,
                    turn_id=turn_id,
                    cancellation=cancellation,
                    channel_id=message.channel_id,
                    config=config,
                    context={},
                ),
                agent_id=agent_id,
            )
            if isinstance(step_decision, RejectRun):
                return TurnOutcome(
                    turn_id=turn_id,
                    status="error",
                    error={
                        "code": "agent-step-rejected",
                        "message": step_decision.reason,
                        "retryable": True,
                    },
                )
            # UserMessage belongs to the Session history boundary, not to the
            # TurnExecutor state machine. Persist it after admission and the
            # before-turn policy, but before any expensive Agent construction
            # or LLM work, so the client receives the durable echo promptly.
            user_message_id = await self._persist_inbound_user_message(
                inbound,
                turn_id=turn_id,
            )
            task = asyncio.create_task(
                self._executor.execute(
                    inbound,
                    turn_id=turn_id,
                    config=config,
                    agent_profile=profile,
                    cancellation=cancellation,
                    user_message_id=user_message_id,
                ),
                name=f"direct-turn:{session_id}:{turn_id}",
            )
            executed = True
            self._direct_tasks[session_id] = task
            self._direct_signals[session_id] = cancellation
            await self._publish_session_status_async(session_id, "running")
            outcome = await task
            if message.request_id:
                await self.completions.complete(session_id, message.request_id, outcome)
            return outcome
        except asyncio.CancelledError:
            outcome = TurnOutcome(turn_id=turn_id, status="cancelled")
            if message.request_id:
                await self.completions.complete(session_id, message.request_id, outcome)
            return outcome
        except Exception as exc:
            logger.exception(
                "[agent-loop] inbound history admission failed session=%s request=%s",
                session_id,
                message.request_id,
            )
            outcome = TurnOutcome(
                turn_id=turn_id,
                status="error",
                error={
                    "code": "user-message-persist-failed",
                    "message": str(exc),
                    "retryable": True,
                },
            )
            if message.request_id:
                await self.completions.complete(session_id, message.request_id, outcome)
            return outcome
        finally:
            self._direct_reservations.discard(session_id)
            self._direct_tasks.pop(session_id, None)
            self._direct_signals.pop(session_id, None)
            if executed:
                try:
                    record = self.agent_registry.ensure(metadata.agent_id or "default")
                    await self._dispatch_agent_hook(
                        AGENT_AFTER_RUN_SPEC,
                        AfterRunPayload(
                            agent=AgentSubject(metadata.agent_id or "default", record.identity),
                            session_id=session_id,
                            turn_id=turn_id,
                            request_id=message.request_id,
                            status=(outcome.status if "outcome" in locals() else "cancelled"),
                            cancellation=cancellation,
                            channel_id=message.channel_id,
                        ),
                        agent_id=metadata.agent_id or "default",
                    )
                except Exception:
                    logger.exception("[agent-loop] direct agent/after-run failed session=%s", session_id)
            try:
                await self._publish_session_status_async(session_id, "idle")
            except Exception:
                logger.debug("[agent-loop] status idle publish failed", exc_info=True)
            completion.set()
            if self._direct_completion_events.get(session_id) is completion:
                self._direct_completion_events.pop(session_id, None)
                self._direct_parent_tasks.pop(session_id, None)

    async def _persist_inbound_user_message(
        self,
        inbound: BusMessage,
        *,
        turn_id: str,
    ) -> str:
        """Commit the admitted user input before the TurnExecutor starts.

        Session history is a data-plane fact owned by the Session projection;
        the AgentLoop only coordinates the existing event sink. Keeping this
        boundary here means TurnExecutor remains a pure Turn/Reply/Tool state
        machine and cannot accidentally persist the same input twice.
        """
        session_id = inbound.from_session or str(inbound.data.get("session_id") or "")
        content = inbound.data.get("content", "")
        if not session_id or not content:
            return ""
        attachments = inbound.data.get("attachments") or []
        user_metadata = {
            "hide": False,
            "agent_id": inbound.metadata.agent_id or "default",
        }
        if inbound.metadata.request_id:
            user_metadata["request_id"] = inbound.metadata.request_id
        persisted_content = build_user_content(
            normalize_stored_user_content(content),
            attachments,
            include_images=True,
        )
        user_event = UserMessageEvent(
            reply_id=turn_id,
            content=from_openai_message({"role": "user", "content": persisted_content}),
            message_metadata=user_metadata,
            data={**inbound.data},
        )
        result = await self.emit_session_event(
            session_id,
            inbound.from_channel,
            user_event,
            metadata=inbound.metadata,
        )
        if not result.persisted_messages:
            raise RuntimeError("Session 未返回已持久化的 UserMessage")
        return result.persisted_messages[0].id

    async def _validate_inbound(self, message: InboundMessage) -> dict | None:
        """Validate the public delivery boundary before writing history."""
        session = await self.session_manager.get_session(message.session_id)
        if session is None:
            return {
                "code": "session_not_found",
                "message": f"会话不存在: {message.session_id}",
                "retryable": False,
            }
        if session["channel_id"] != message.channel_id:
            return {
                "code": "channel_mismatch",
                "message": (
                    f"会话通道为 {session['channel_id']}，消息路由通道为 "
                    f"{message.channel_id}"
                ),
                "retryable": False,
            }
        return None

    async def delete_session(self, session_id: str) -> None:
        """删除 active Turn 后交给 SessionService；Inbox 监听 disposed Hook 清理自身。"""
        meta = await self.session_manager.get_session_metadata(session_id)
        member_ids: list[str] = []
        teams = meta.get("teams") if isinstance(meta, dict) else None
        if isinstance(teams, dict):
            for team in teams.values():
                if isinstance(team, dict) and isinstance(team.get("members"), dict):
                    member_ids.extend(str(sid) for sid in team["members"])
        # AgentService 只拥有 active Turn；pending 的清理由 ftre-inbox Package
        # 在 Session 删除事件中负责。这里必须先取消并等待当前 active 完整收尾，
        # 再删除历史，否则 Reply 的最终 update_message 会访问已删除的索引。
        for member_id in (session_id, *member_ids):
            await self._cancel_session_and_wait(member_id)
        await self.session_manager.delete_session(session_id)

    async def stop(self) -> None:
        """优雅关闭：取消 active Run；Hook scope 由 Plugin Fiber 管理。"""
        direct_tasks = tuple(self._direct_tasks.values())
        for task in direct_tasks:
            task.cancel()
        if direct_tasks:
            await asyncio.gather(*direct_tasks, return_exceptions=True)
        self._direct_tasks.clear()
        self._direct_signals.clear()
        for completion in self._direct_completion_events.values():
            completion.set()
        self._direct_completion_events.clear()
        self._direct_parent_tasks.clear()
        self._direct_reservations.clear()
        await self.completions.close()

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
    ):
        """Delegate Session event persistence and broadcast to its sole Owner."""
        if self.session_events is None:
            raise RuntimeError("SessionEventService is not available")
        return await self.session_events.emit(
            session_id,
            channel_id,
            event,
            metadata=metadata,
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
        if not events:
            return TurnOutcome(
                turn_id="",
                status="error",
                error={"code": "confirmation_events_required", "message": "缺少确认事件"},
            )
        if self.is_active_session(session_id):
            return TurnOutcome(
                turn_id="",
                status="error",
                error={"code": "agent-busy", "message": "Session 当前仍在执行", "retryable": True},
            )
        turn_id = f"confirm_{uuid.uuid4().hex[:12]}"
        cancellation = asyncio.Event()
        config, profile = await self._executor.resolve_inbound_config(inbound, turn_id=turn_id)
        task = asyncio.create_task(
            self._executor.execute(
                inbound,
                turn_id=turn_id,
                config=config,
                agent_profile=profile,
                cancellation=cancellation,
                confirm_event=events[-1],
            ),
            name=f"confirmation:{session_id}:{turn_id}",
        )
        self._direct_tasks[session_id] = task
        self._direct_signals[session_id] = cancellation
        try:
            return await task
        except asyncio.CancelledError:
            return TurnOutcome(turn_id=turn_id, status="cancelled")
        finally:
            self._direct_tasks.pop(session_id, None)
            self._direct_signals.pop(session_id, None)
            try:
                await self._publish_session_status_async(session_id, "idle")
            except Exception:
                logger.debug("[agent-loop] confirmation idle status publish failed", exc_info=True)

    async def _publish_session_status_async(self, session_id: str, status: str) -> None:
        """发布独立于 pending 的 Session activity 状态。"""
        session = await self.session_manager.get_session(session_id)
        if not session:
            # 删除 Session 后，active Turn 的 finally 仍可能运行到这里；历史已
            # 经不存在时没有合法的目标 Channel，也不应向空 to_channel 投递消息。
            logger.debug(
                "[agent-loop] skip session status for deleted session=%s status=%s",
                session_id,
                status,
            )
            return
        channel_id = session.get("channel_id", "")
        if not channel_id:
            logger.debug(
                "[agent-loop] skip session status without channel session=%s status=%s",
                session_id,
                status,
            )
            return
        await self.bus.publish_outbound(
            BusMessage(
                type="session/status",
                from_channel=channel_id,
                to_channel=channel_id,
                from_session=session_id,
                to_session=session_id,
                data={"session_id": session_id, "status": status},
            )
        )
