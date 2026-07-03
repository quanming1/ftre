"""
AgentLoop - 全局单例，消费所有 session 的 inbound 消息

职责：
- 从 Bus 全局 inbound 队列消费消息
- 收到 user_message 时，加载历史 → 驱动 ReActAgent → 将事件逐条发布到 outbound
- 系统级指令（如 /cancel）在锁外立即执行

并发模型（v3 — 主循环化）：
- _consume 不 await pipeline，而是 create_task(_dispatch(msg)) 立即返回
- _dispatch 对不同 session 并发执行，对同一 session 用 asyncio.Lock 串行
- 系统级指令绕过 session lock，在锁外直接执行
- Agent 执行全在主事件循环，Task.cancel() 在 LLM stream 的 await 处立即生效
"""
import asyncio
import copy
import logging
import os
import uuid
from concurrent.futures import Future

from ftre_agent_core.agent import ReActAgent
from ftre_agent_core import Tracer
from ftre_agent_core.agent.event import (
    AgentEvent,
    DoneEvent,
    DoneReason,
    ErrorEvent,
    AssistantMessageCompleteEvent,
    ReasoningCompleteEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageUpdateEvent,
    UserMessageEvent,
)
from ftre.bus import BusMessage, EventBus, GLOBAL_CHANNEL, GLOBAL_SESSION
from ftre.channel.subagent_channel import SUBAGENT_CHANNEL_ID
from ftre.config import AgentConfig, load_config
from ftre.session import SessionManager
from ftre.session.multimodal import build_user_content, normalize_stored_user_content
from ftre.tools import ToolRegistry
from ftre.tools._workspace import WorkspaceAccessor
from ftre.utils import Pipeline
from ftre.trace_store import SQLiteTraceExporter, TRACE_DB_PATH
from .compact_manager import CompactManager

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    全局单例，消费所有 session 的消息。

    并发模型：
    - _consume：只负责从 inbound 队列取消息，create_task 派发后立即取下一条
    - _dispatch：per-session asyncio.Lock 保证同一 session 串行，不同 session 并发
    - 系统级指令（如 /cancel）：绕过 session lock，在锁外直接执行
    - 所有 Agent 执行在主事件循环，Task.cancel() 可在 LLM stream 的 await 处立即生效

    生命周期：
    - start()  → 启动消费协程
    - stop()   → 取消消费协程 + 中断 Agent + 取消所有 dispatch task
    """

    def __init__(
        self,
        bus: EventBus,
        session_manager: SessionManager,
        channel_manager=None,
        config: AgentConfig = None,
        hook_manager=None,
        tool_registry: ToolRegistry | None = None,
        command_manager=None,
        plugin_manager=None,
        agent_manager=None,
    ):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.hook_manager = hook_manager
        self.tool_registry = tool_registry
        self.command_manager = command_manager
        self.plugin_manager = plugin_manager
        self.agent_manager = agent_manager
        self._injected_config = config
        self._task: asyncio.Task | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self.tracer = Tracer([SQLiteTraceExporter(TRACE_DB_PATH)])

        # ─── 并发控制 ──────────────────────────────────────────
        # per-session 协程锁：保证同一 session 的消息串行处理，
        # 不同 session 的消息可以并发执行。
        self._session_locks: dict[str, asyncio.Lock] = {}

        # 当前正在执行的 Agent（session_id → ReActAgent）
        # 主循环化后所有访问都在主循环协程内，无需 threading.Lock
        self._active_agents: dict[str, ReActAgent] = {}

        # subagent session_id → task 工具等待的一次性完成结果
        self._subagent_done_futures: dict[str, Future[dict]] = {}

        # session_id → 该 session 当前正在执行的 dispatch task
        # cancel 时通过 task.cancel() 立即中断
        self._session_tasks: dict[str, asyncio.Task] = {}

        # 并发派发任务集合：_consume 创建的 dispatch task 都注册到这里，
        # stop() 时可以统一 cancel，防止任务飞丢。
        self._dispatch_tasks: set[asyncio.Task] = set()

        # 后台 idle compact 状态标记：手动 /compact 执行中的 session
        self._compacting_sessions: set[str] = set()

        # ─── pipeline ─────────────────────────────────────────
        self._pipeline = Pipeline("consume")
        self._pipeline.use(self._step_command, name="command")
        self._pipeline.use(self._step_compact, name="compact")
        self._pipeline.use(self._step_run, name="run")

        self.compact_manager = CompactManager(
            session_manager=self.session_manager,
            channel_manager=self.channel_manager,
            bus=self.bus,
            threshold=self._initial_context_cfg().compact_threshold,
        )

        self._register_commands()

    def _initial_context_cfg(self):
        """实例化时读一次 ContextConfig 用于 CompactManager 默认参数。"""
        try:
            cfg = self._load_current_config()
            return cfg.context
        except Exception:
            from ftre.config import ContextConfig
            return ContextConfig()

    def _register_commands(self) -> None:
        """注册内置斜杠指令。"""
        if self.command_manager is None:
            return
        # /cancel：系统级指令，在锁外执行，立即取消当前 session 的 Agent
        def _on_cancel(ctx):
            sid = ctx.meta["inbound"].from_session or ctx.meta["inbound"].data.get("session_id", "")
            agent = self._active_agents.get(sid)
            if agent:
                agent.cancel_nowait()
            task = self._session_tasks.get(sid)
            if task and not task.done():
                task.cancel()
                logger.info(f"[agent-loop] cancel task 已取消 session={sid}")
        self.command_manager.register(
            "/cancel",
            _on_cancel,
            description="取消当前会话执行",
            system=True,
        )
        # /compact：普通指令，在锁内执行，串行安全
        self.command_manager.register(
            "/compact",
            self._cmd_compact,
            description="压缩当前会话上下文",
        )

    async def _cmd_compact(self, ctx) -> None:
        """/compact 指令：在当前位置直接执行压缩，执行完 pipeline 自动短路。"""
        inbound = ctx.meta["inbound"]
        session_id = inbound.from_session
        channel_id = inbound.from_channel

        self._compacting_sessions.add(session_id)
        await self._publish_session_status_async(session_id, "compacting")

        try:
            config = self._load_current_config()

            # 先尝试启用 pending compact（秒级，不用调 LLM）
            enabled = await self.compact_manager.enable_pending_compact(
                session_id, channel_id,
                config=config,
                silent=False,
            )

            # 没有 pending → 直接压缩（enabled=True）
            if not enabled:
                await self.compact_manager.compact(
                    session_id, channel_id,
                    config=config,
                    silent=False,
                    enabled=True,
                )
        except Exception:
            logger.exception(f"[agent-loop] /compact 执行异常 session={session_id}")
        finally:
            self._compacting_sessions.discard(session_id)
            await self._publish_session_status_async(session_id, self.get_session_status(session_id))

    def start(self) -> None:
        """启动消费循环"""
        self._event_loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._consume())

    def is_session_running(self, session_id: str) -> bool:
        """该 session 是否有正在跑的 ReActAgent。"""
        return session_id in self._active_agents

    def get_session_status(self, session_id: str) -> str:
        """返回客户端可见状态：idle / running / compacting。"""
        if session_id in self._active_agents:
            return "running"
        if session_id in self._compacting_sessions:
            return "compacting"
        return "idle"

    def register_subagent_done_future(self, session_id: str, future: Future[dict]) -> bool:
        """注册 subagent 单轮执行完成通知；返回 False 表示已有等待者。"""
        existing = self._subagent_done_futures.get(session_id)
        if existing is not None and not existing.done():
            return False
        self._subagent_done_futures[session_id] = future
        return True

    def unregister_subagent_done_future(
        self,
        session_id: str,
        future: Future[dict] | None = None,
    ) -> None:
        """移除未完成的 subagent 等待者，避免启动/执行超时后残留。"""
        existing = self._subagent_done_futures.get(session_id)
        if existing is not None and (future is None or existing is future):
            self._subagent_done_futures.pop(session_id, None)

    async def stop(self) -> None:
        """优雅关闭：取消消费循环 + 中断所有 Agent。"""
        # 1. 取消消费循环
        if self._task:
            self._task.cancel()

        # 2. 取消所有 dispatch task（等锁的也会被唤醒退出）
        for t in list(self._dispatch_tasks):
            t.cancel()

        # 3. 取消所有正在运行的 Agent
        for agent in self._active_agents.values():
            agent.cancel_nowait()
        self._active_agents.clear()
        self._session_tasks.clear()

        # 4. 取消所有后台 idle 压缩 task
        self.compact_manager.cancel_all_compact_tasks()
        for sid, future in self._subagent_done_futures.items():
            if not future.done():
                future.set_result(
                    {
                        "session_id": sid,
                        "channel_id": SUBAGENT_CHANNEL_ID,
                        "status": "cancelled",
                        "final_content": "",
                    }
                )
        self._subagent_done_futures.clear()

    # ─── 消费循环 ────────────────────────────────────────────

    async def _consume(self) -> None:
        """消费循环：从 inbound 队列取消息，并发派发到 _dispatch。"""
        try:
            async for msg in self.bus.subscribe_inbound():
                try:
                    data = {"inbound": msg}
                    task = asyncio.create_task(self._dispatch(data))
                    # 注册到 dispatch_tasks 集合，stop() 时可统一 cancel
                    self._dispatch_tasks.add(task)
                    task.add_done_callback(self._dispatch_tasks.discard)
                except Exception:
                    logger.exception("[agent-loop] 派发 dispatch 异常，已丢弃该消息")
        except asyncio.CancelledError:
            pass

    async def _dispatch(self, data: dict) -> None:
        """单条消息的派发入口。

        处理顺序：
        1. 系统级指令（如 /cancel）→ 在锁外执行，不受阻塞
        2. 普通消息 → 获取 session lock → 串行执行 pipeline

        系统级指令在锁外执行，保证用户点击停止后立即响应。
        CancelledError 会在 LLM stream 的下一个 await 处立即抛出。
        """
        # ─── 系统级指令：锁外执行 ───
        if await self.command_manager.try_dispatch_system(data):
            return

        # ─── 普通消息：获取 per-session lock → 跑 pipeline ───
        inbound = data["inbound"]
        session_id = inbound.data.get("session_id", "") or inbound.from_session

        if session_id:
            # 注册 session → task 映射（cancel 时用）
            current_task = asyncio.current_task()
            self._session_tasks[session_id] = current_task

            lock = self._session_locks.setdefault(session_id, asyncio.Lock())
            try:
                async with lock:
                    await self._pipeline.run(data)
            except asyncio.CancelledError:
                # cancel 导致的 CancelledError，正常退出
                logger.info(f"[agent-loop] session={session_id} 被 cancel 中断")
            finally:
                # 清理 session → task 映射
                if self._session_tasks.get(session_id) is current_task:
                    self._session_tasks.pop(session_id, None)
        else:
            # 无 session_id 的消息直接跑 pipeline（不需要锁）
            await self._pipeline.run(data)

    # ─── pipeline 各阶段 ────────────────────────────────────

    async def _step_command(self, data: dict) -> bool:
        """普通指令预处理：匹配普通指令（锁内）。

        系统级指令已在 _dispatch 锁外处理，这里只处理普通指令（如 /compact）。
        流程：先判断是否命中 → 命中则持久化 user_message → 再执行指令 handler。
        返回 True 继续 pipeline（未匹配），返回 False 短路（已执行）。
        """
        if not self.command_manager:
            return True

        # 1. 先判断是否命中（不执行 handler）
        cmd_def = self.command_manager.match(data)
        if cmd_def is None:
            return True

        # 2. 命中 → 先持久化用户输入（格式与 _run_async Step 6 对齐）
        inbound = data["inbound"]
        session_id = inbound.from_session or inbound.data.get("session_id", "")
        content = inbound.data.get("content", "")
        if session_id and content:
            try:
                await self.session_manager.save_message(
                    session_id, "user_message", {
                        "event_id": uuid.uuid4().hex[:16],
                        "content": normalize_stored_user_content(content),
                        "metadata": {"hide": False},
                    },
                )
            except Exception:
                logger.exception(f"[agent-loop] 指令消息持久化失败 session={session_id}")

        # 3. 再执行指令 handler
        await self.command_manager.try_dispatch(data)
        return False

    async def _step_compact(self, data: dict) -> bool:
        """压缩阶段：只判断是否需要自动压缩，把结论写入 data['need_compact']。"""
        inbound = data["inbound"]
        if inbound.type != "user_message":
            return True
        session_id = inbound.data.get("session_id", "") or inbound.from_session
        if not session_id:
            return True
        channel_id = inbound.from_channel

        try:
            config = self._load_current_config()
            need = await self.compact_manager.should_compact(
                session_id,
                channel_id,
                config,
                threshold=getattr(config.context, "precompact_threshold", 0.5),
            )
            if need:
                data["need_compact"] = True
                logger.info(f"[agent-loop] 需要关键路径压缩 session={session_id}")
        except Exception:
            logger.exception(f"[agent-loop] should_compact 异常 session={session_id}")

        return True

    async def _step_run(self, data: dict) -> bool:
        """执行阶段：在主事件循环直接 await Agent 运行。

        主循环化后，Agent 的 LLM stream 在主循环的 await 处让出控制权，
        Task.cancel() 的 CancelledError 在下一个 LLM chunk await 处立即抛出，
        实现毫秒级响应的 cancel。
        """
        inbound = data["inbound"]
        need_compact = bool(data.get("need_compact"))

        await self._run_async(inbound, need_compact)
        return False

    # ─── Agent 执行 ─────────────────────────────────────────

    # 需要持久化的事件类型（dataclass 类型，用 isinstance 检查）
    _PERSISTENT_CLASSES: tuple[type, ...] = (
        AssistantMessageCompleteEvent,
        ReasoningCompleteEvent,
        ToolCallEvent,
        ToolResultEvent,
        DoneEvent,
        UsageUpdateEvent,
        ErrorEvent,
        UserMessageEvent,
    )

    async def _run_async(self, inbound: BusMessage, need_compact: bool = False) -> None:
        """异步执行 Agent，事件逐条投递到 Bus。

        主循环化后跑在主事件循环里，不再需要 executor 线程。
        DB / Bus 操作直接 await，不再 run_coroutine_threadsafe。
        """
        # Step 1: 入参校验
        content = inbound.data.get("content", "")
        attachments = inbound.data.get("attachments") or []
        session_id = inbound.data.get("session_id", "")

        if not content and not attachments:
            return
        if not session_id:
            logger.warning("[agent-loop] 收到无 session_id 的消息，忽略")
            return

        # Step 1.5: 解析 agent_id，加载 per-agent 配置
        agent_id = (inbound.metadata or {}).get("agent_id", "") or "default"
        agent_profile = None
        if self.agent_manager is not None:
            agent_profile = self.agent_manager.load(agent_id)

        # Step 2: 鉴权
        session = await self.session_manager.get_session(session_id)
        if session is None:
            logger.warning(f"[agent-loop] session 不存在，拒绝执行: session={session_id}")
            return
        if session["channel_id"] != inbound.from_channel:
            logger.warning(
                f"[agent-loop] session 与 channel 不匹配: "
                f"session={session_id} (channel={session['channel_id']}), 消息来自 {inbound.from_channel}"
            )
            return

        # Step 2.5: 并发防御 — 已由 per-session asyncio.Lock 保证同一 session 串行，
        # 但保留防御性日志。
        if session_id in self._active_agents:
            existing = self._active_agents[session_id]
            logger.error(
                f"[agent-loop] ⚠️ session lock 未能防止并发: "
                f"session={session_id}, existing_agent={existing!r}。"
                f"这不应该发生，请检查 _dispatch 的 session lock 逻辑。"
            )
            existing.cancel_nowait()

        # Step 2.8: 关键路径压缩
        config = self._load_current_config()
        # 如果有 per-agent 配置，覆盖 llm 和 workspace
        if agent_profile is not None:
            config = copy.deepcopy(config)
            config.llm = agent_profile.llm
            if agent_profile.workspace:
                config.workspace = agent_profile.workspace
        if need_compact:
            try:
                silent = getattr(config.context, "silent", True)
                # 先尝试启用 pending compact（秒级）
                enabled = await self.compact_manager.enable_pending_compact(
                    session_id, inbound.from_channel,
                    config=config,
                    silent=silent,
                )
                if not enabled:
                    # 没有 pending → 直接压缩
                    await self.compact_manager.compact(
                        session_id, inbound.from_channel,
                        config=config,
                        silent=silent,
                        enabled=True,
                    )
            except Exception:
                logger.exception(f"[agent-loop] 关键路径压缩异常 session={session_id}")

        # Step 4: 加载历史消息 + hook
        workspace = session.get("workspace", "") or os.getcwd()
        if agent_profile and agent_profile.workspace:
            workspace = agent_profile.workspace
        messages, hook_config = await self._build_messages(
            session_id,
            content,
            attachments,
            config,
            inbound_data=inbound.data,
            channel_id=inbound.from_channel,
            workspace=workspace,
            agent_dir=(agent_profile.agent_dir if agent_profile else ""),
        )

        # Step 5: 创建 Agent + 注册到 _active_agents
        assert self.agent_manager is not None, "agent_manager must be provided"
        agent = self.agent_manager.create_agent(
            profile=agent_profile,
            config=hook_config,
            channel_manager=self.channel_manager,
            tool_registry=self.tool_registry,
            tracer=self.tracer,
            channel_id=inbound.from_channel,
            session_id=session_id,
        )
        self._active_agents[session_id] = agent
        await self._publish_session_status_async(session_id, "running")

        # Step 6: 持久化用户输入
        # 归一化存储格式：只保留 UI/DB 可回放的 user parts。
        # LLM API 格式在构建上下文时由 build_user_content() 统一转换。
        stored_content = normalize_stored_user_content(content)
        user_event_id = uuid.uuid4().hex[:16]
        stored_user_data = {
            "event_id": user_event_id,
            "content": stored_content,
            "attachments": attachments,
            "metadata": {"hide": False},
        }
        await self.session_manager.save_message(
            session_id,
            "user_message",
            stored_user_data,
        )

        # Step 6.5: echo user_message 给前端
        # 透传 inbound.metadata（含 frame_id），前端用 frame_id 与本地乐观占位去重
        echo = BusMessage(
            type="agent_event",
            from_channel=inbound.from_channel,
            to_channel=inbound.to_channel,
            from_session=inbound.from_session,
            to_session=inbound.to_session,
            data={
                "type": "user_message",
                "event_id": user_event_id,
                "data": {**inbound.data, "event_id": user_event_id},
            },
            metadata=inbound.metadata,
        )
        await self.bus.publish_outbound(echo)

        # Step 7: 构建运行时上下文（工具共享数据）
        runtime_context = {
            "session_id": session_id,
            "channel_id": inbound.from_channel,
            "event_loop": self._event_loop,
            "session_manager": self.session_manager,
            "bus": self.bus,
            "agent_loop": self,
            "llm_config": hook_config.llm,
            "workspace": WorkspaceAccessor(
                session_id=session_id,
                session_manager=self.session_manager,
                event_loop=self._event_loop,
                fallback_cwd=workspace,
            ),
            "trace_name": f"session:{session_id}",
            "trace_tags": [inbound.from_channel or "unknown"],
            "trace_metadata": {
                "session_id": session_id,
                "channel_id": inbound.from_channel,
                "workspace": workspace,
            },
        }

        # Step 7.5: before_agent_run hook（插件注入对话上下文 / 系统身份）
        if self.hook_manager is not None:
            from ftre.plugin import AgentRunContext, BEFORE_AGENT_RUN
            ctx = AgentRunContext(
                session_id=session_id,
                channel_id=inbound.from_channel,
                messages=messages,
                config=hook_config,
            )
            ctx = self.hook_manager.trigger_sync(BEFORE_AGENT_RUN, ctx)
            messages = ctx.messages

        subagent_status = "completed"
        final_content = ""

        try:
            async for event in agent.run(messages, runtime_context=runtime_context):
                # task 工具只使用这里记录的最后一条完整 assistant 回复作为返回内容。
                if isinstance(event, AssistantMessageCompleteEvent):
                    final_content = event.content or ""

                # 检查 cancel：Task.cancel() 会在 await 处抛 CancelledError，
                # 但 async for 的 yield 不一定被 cancel 打断（取决于 generator 内部实现），
                # 所以在这里也检查 CancellationToken 作为补充。
                if isinstance(event, self._PERSISTENT_CLASSES):
                    event_data = event._data_dict()
                    event_data["event_id"] = event.event_id
                    await self.session_manager.save_message(
                        session_id, event.type.value, event_data
                    )

                out = BusMessage(
                    type="agent_event",
                    from_channel=inbound.from_channel,
                    to_channel=inbound.to_channel,
                    from_session=inbound.from_session,
                    to_session=inbound.to_session,
                    data=event.to_dict(),
                )
                await self.bus.publish_outbound(out)
                # usage_update 时检查是否需要预压缩。
                # maybe_schedule_idle_compact 自带去重，确保同一 session 只有一个 compact task 在飞，
                # 所以即使 usage_update 频繁也不会反复调度。
                if isinstance(event, UsageUpdateEvent) and inbound.from_channel != SUBAGENT_CHANNEL_ID:
                    try:
                        _cfg = self._load_current_config()
                        await self.compact_manager.maybe_schedule_idle_compact(session_id, inbound.from_channel, _cfg)
                    except Exception:
                        logger.debug("[agent-loop] 调度 usage 压缩失败", exc_info=True)

        except asyncio.CancelledError:
            subagent_status = "cancelled"
            # Task.cancel() 导致的 CancelledError：Agent 被 cancel 中断
            logger.info(f"[agent-loop] Agent 被 cancel 中断 session={session_id}")
            # 发送 done 事件让前端知道已停止
            done_evt = BusMessage(
                type="agent_event",
                from_channel=inbound.from_channel,
                to_channel=inbound.to_channel,
                from_session=inbound.from_session,
                to_session=inbound.to_session,
                data=DoneEvent(success=False, reason=DoneReason.CANCELLED).to_dict(),
            )
            await self.bus.publish_outbound(done_evt)
        except Exception:
            subagent_status = "error"
            logger.exception(f"[agent-loop] _run 异常 (session={session_id})")
            err_evt = BusMessage(
                type="agent_event",
                from_channel=inbound.from_channel,
                to_channel=inbound.to_channel,
                from_session=inbound.from_session,
                to_session=inbound.to_session,
                data=DoneEvent(success=False, reason=DoneReason.ERROR).to_dict(),
            )
            await self.bus.publish_outbound(err_evt)
        finally:
            # 只有当前 agent 仍是注册的那个才清理。
            if self._active_agents.get(session_id) is agent:
                self._active_agents.pop(session_id, None)
                should_emit_idle = True
            else:
                should_emit_idle = False

            if inbound.from_channel == SUBAGENT_CHANNEL_ID:
                future = self._subagent_done_futures.pop(session_id, None)
                if future is not None and not future.done():
                    # finally 覆盖正常结束、异常和 cancel，是父 task 唤醒的唯一出口。
                    future.set_result(
                        {
                            "session_id": session_id,
                            "channel_id": inbound.from_channel,
                            "status": subagent_status,
                            "final_content": final_content,
                        }
                    )

            if should_emit_idle:
                await self._publish_session_status_async(session_id, "idle")

            # 步骤 5（无感）：本轮结束后调度后台空闲压缩。
            if inbound.from_channel != SUBAGENT_CHANNEL_ID:
                try:
                    _cfg = self._load_current_config()
                    await self.compact_manager.maybe_schedule_idle_compact(session_id, inbound.from_channel, _cfg)
                except Exception:
                    logger.debug("[agent-loop] 调度 idle 压缩失败", exc_info=True)

    # ─── 工具方法 ──────────────────────────────────────────

    async def _publish_session_status_async(self, session_id: str, status: str) -> None:
        """广播 session 运行态变化（异步版）。"""
        evt = BusMessage(
            type="global_event",
            from_channel=GLOBAL_CHANNEL,
            to_channel=GLOBAL_CHANNEL,
            from_session=GLOBAL_SESSION,
            to_session=GLOBAL_SESSION,
            data={
                "type": "session_status",
                "data": {"session_id": session_id, "status": status},
            },
        )
        await self.bus.publish_outbound(evt)

    def _load_current_config(self) -> AgentConfig:
        """读取当前生效的配置"""
        if self._injected_config is not None:
            return self._injected_config
        return load_config()

    async def _build_messages(
        self,
        session_id: str,
        content: str,
        attachments: list[dict],
        config: AgentConfig,
        *,
        inbound_data: dict | None = None,
        channel_id: str = "",
        workspace: str = "",
        agent_dir: str = "",
    ) -> tuple[list[dict], AgentConfig]:
        """构建 LLM 输入消息，触发 before_messages_build hook。"""
        events = await self.session_manager.get_messages_by_session(session_id)

        # 触发 before_messages_build hook（插件做孤立事件清理、相邻性修复、裁剪、标题生成等）
        hook_config = copy.deepcopy(config)
        if self.hook_manager is not None:
            from ftre.plugin import MessagesBuildContext, BEFORE_MESSAGES_BUILD
            ctx = MessagesBuildContext(
                session_id=session_id,
                channel_id=channel_id,
                inbound_data=inbound_data or {},
                workspace=workspace,
                agent_dir=agent_dir,
                config=hook_config,
                events=events,
            )
            ctx = self.hook_manager.trigger_sync(BEFORE_MESSAGES_BUILD, ctx)
            hook_config = ctx.config
            events = ctx.events

        user_content = build_user_content(
            content,
            attachments,
            include_images=hook_config.llm.vision,
        )

        if events:
            prune_opts = {
                "protect_turns": 2,
                "max_chars": 2000,
                "head_chars": 1000,
                "tail_chars": 1000,
            }
            history = SessionManager.to_openai_messages(
                events,
                config={"llm": {"vision": hook_config.llm.vision}},
                prune=prune_opts,
            )
            history.append({"role": "user", "content": user_content})
            return history, hook_config

        return [{"role": "user", "content": user_content}], hook_config
