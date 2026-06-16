"""
AgentLoop - 全局单例，消费所有 session 的 inbound 消息

职责：
- 从 Bus 全局 inbound 队列消费消息
- 收到 user_input 时，加载历史 → 驱动 ReActAgent → 将事件逐条发布到 outbound
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

from ftre_agent_core.agent import ReActAgent
from ftre.bus import BusMessage, EventBus, GLOBAL_CHANNEL, GLOBAL_SESSION
from ftre.channel.subagent_channel import SUBAGENT_CHANNEL_ID
from ftre.config import AgentConfig, load_config
from ftre.session import SessionManager
from ftre.session.multimodal import build_user_content
from ftre.tools import ToolRegistry, build_default_tools
from ftre.tools._workspace import WorkspaceAccessor
from ftre.utils import Pipeline
from .compact_handler import CompactHandler

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
        mcp_manager=None,
    ):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.hook_manager = hook_manager
        self.tool_registry = tool_registry
        self.command_manager = command_manager
        self.mcp_manager = mcp_manager
        self._injected_config = config
        self._task: asyncio.Task | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None

        # ─── 并发控制 ──────────────────────────────────────────
        # per-session 协程锁：保证同一 session 的消息串行处理，
        # 不同 session 的消息可以并发执行。
        self._session_locks: dict[str, asyncio.Lock] = {}

        # 当前正在执行的 Agent（session_id → ReActAgent）
        # 主循环化后所有访问都在主循环协程内，无需 threading.Lock
        self._active_agents: dict[str, ReActAgent] = {}

        # session_id → 该 session 当前正在执行的 dispatch task
        # cancel 时通过 task.cancel() 立即中断
        self._session_tasks: dict[str, asyncio.Task] = {}

        # 并发派发任务集合：_consume 创建的 dispatch task 都注册到这里，
        # stop() 时可以统一 cancel，防止任务飞丢。
        self._dispatch_tasks: set[asyncio.Task] = set()

        # 后台 idle compact task 去重：session_id → asyncio.Task
        # 同一 session 同一时间只允许一个 compact task 在飞
        self._compact_tasks: dict[str, asyncio.Task] = {}

        # ─── pipeline ─────────────────────────────────────────
        self._pipeline = Pipeline("consume")
        self._pipeline.use(self._step_command, name="command")
        self._pipeline.use(self._step_compact, name="compact")
        self._pipeline.use(self._step_run, name="run")

        self.compact_handler = CompactHandler(
            session_manager=self.session_manager,
            channel_manager=self.channel_manager,
            bus=self.bus,
            threshold=self._initial_context_cfg().compact_threshold,
        )

        self._register_commands()

    def _initial_context_cfg(self):
        """实例化时读一次 ContextConfig 用于 CompactHandler 默认参数。"""
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

        # emit running
        await self._publish_session_status_async(session_id, "running")

        try:
            config = self._load_current_config()

            # 先尝试启用 pending compact（秒级，不用调 LLM）
            enabled = await self.compact_handler.enable_pending_compact(
                session_id, channel_id,
                config=config,
                silent=False,
            )

            # 没有 pending → 直接压缩（enabled=True）
            if not enabled:
                await self.compact_handler.compact(
                    session_id, channel_id,
                    config=config,
                    silent=False,
                    enabled=True,
                )
        except Exception:
            logger.exception(f"[agent-loop] /compact 执行异常 session={session_id}")
        finally:
            # emit idle
            await self._publish_session_status_async(session_id, "idle")

    def start(self) -> None:
        """启动消费循环"""
        self._event_loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._consume())

    def is_session_running(self, session_id: str) -> bool:
        """该 session 是否有正在跑的 ReActAgent。"""
        return session_id in self._active_agents

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
        返回 True 继续 pipeline，返回 False 短路。
        """
        if not self.command_manager:
            return True
        return not await self.command_manager.try_dispatch(data)

    async def _step_compact(self, data: dict) -> bool:
        """压缩阶段：只判断是否需要自动压缩，把结论写入 data['need_compact']。"""
        inbound = data["inbound"]
        if inbound.type != "user_input":
            return True
        session_id = inbound.data.get("session_id", "") or inbound.from_session
        if not session_id:
            return True
        channel_id = inbound.from_channel

        try:
            config = self._load_current_config()
            need = await self.compact_handler.should_compact(
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

    # 需要持久化的事件类型
    PERSISTENT_EVENTS = {
        "message_complete",
        "reasoning_complete",
        "tool_call",
        "tool_result",
        "tool_cancel_requested",
        "tool_cancelled",
        "done",
        "usage_update",
        "error",
    }

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
        if need_compact:
            try:
                silent = getattr(config.context, "silent", True)
                # 先尝试启用 pending compact（秒级）
                enabled = await self.compact_handler.enable_pending_compact(
                    session_id, inbound.from_channel,
                    config=config,
                    silent=silent,
                )
                if not enabled:
                    # 没有 pending → 直接压缩
                    await self.compact_handler.compact(
                        session_id, inbound.from_channel,
                        config=config,
                        silent=silent,
                        enabled=True,
                    )
            except Exception:
                logger.exception(f"[agent-loop] 关键路径压缩异常 session={session_id}")

        # Step 4: 加载历史消息 + hook
        workspace = session.get("workspace", "") or os.getcwd()
        messages, hook_config = await self._build_messages(
            session_id,
            content,
            attachments,
            config,
            inbound_data=inbound.data,
            channel_id=inbound.from_channel,
            workspace=workspace,
        )

        # Step 5: 创建 Agent + 注册到 _active_agents
        agent = self._create_agent(hook_config)
        agent.system_prompt = (
            hook_config.system_prompt
            + f"\n\n[当前上下文] channel_id={inbound.from_channel}, session_id={session_id}"
        )
        self._active_agents[session_id] = agent
        await self._publish_session_status_async(session_id, "running")

        # Step 6: 持久化用户输入
        await self.session_manager.save_message(session_id, "USER_INPUT", inbound.data)

        # Step 6.5: echo user_input 给前端
        # 透传 inbound.metadata（含 frame_id），前端用 frame_id 与本地乐观占位去重
        echo = BusMessage(
            type="agent_event",
            from_channel=inbound.from_channel,
            to_channel=inbound.to_channel,
            from_session=inbound.from_session,
            to_session=inbound.to_session,
            data={"type": "user_input", "data": inbound.data},
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
            "workspace": WorkspaceAccessor(
                session_id=session_id,
                session_manager=self.session_manager,
                event_loop=self._event_loop,
                fallback_cwd=workspace,
            ),
        }

        try:
            async for event in agent.run(messages, runtime_context=runtime_context):
                # 检查 cancel：Task.cancel() 会在 await 处抛 CancelledError，
                # 但 async for 的 yield 不一定被 cancel 打断（取决于 generator 内部实现），
                # 所以在这里也检查 CancellationToken 作为补充。
                if event.get("type") in self.PERSISTENT_EVENTS:
                    await self.session_manager.save_message(
                        session_id, event["type"], event.get("data", {})
                    )

                out = BusMessage(
                    type="agent_event",
                    from_channel=inbound.from_channel,
                    to_channel=inbound.to_channel,
                    from_session=inbound.from_session,
                    to_session=inbound.to_session,
                    data=event,
                )
                await self.bus.publish_outbound(out)
                # usage_update 时检查是否需要预压缩。
                # _compact_tasks 去重保护确保同一 session 只有一个 compact task 在飞，
                # 所以即使 usage_update 频繁也不会反复调度。
                if event.get("type") == "usage_update" and inbound.from_channel != SUBAGENT_CHANNEL_ID:
                    try:
                        await self._schedule_idle_compact(session_id, inbound.from_channel)
                    except Exception:
                        logger.debug("[agent-loop] 调度 usage 压缩失败", exc_info=True)

        except asyncio.CancelledError:
            # Task.cancel() 导致的 CancelledError：Agent 被 cancel 中断
            logger.info(f"[agent-loop] Agent 被 cancel 中断 session={session_id}")
            # 发送 done 事件让前端知道已停止
            done_evt = BusMessage(
                type="agent_event",
                from_channel=inbound.from_channel,
                to_channel=inbound.to_channel,
                from_session=inbound.from_session,
                to_session=inbound.to_session,
                data={"type": "done", "data": {"success": False, "reason": "cancelled"}},
            )
            await self.bus.publish_outbound(done_evt)
        except Exception:
            logger.exception(f"[agent-loop] _run 异常 (session={session_id})")
            err_evt = BusMessage(
                type="agent_event",
                from_channel=inbound.from_channel,
                to_channel=inbound.to_channel,
                from_session=inbound.from_session,
                to_session=inbound.to_session,
                data={"type": "done", "data": {"success": False, "reason": "error"}},
            )
            await self.bus.publish_outbound(err_evt)
        finally:
            # 只有当前 agent 仍是注册的那个才清理。
            if self._active_agents.get(session_id) is agent:
                self._active_agents.pop(session_id, None)
                should_emit_idle = True
            else:
                should_emit_idle = False

            if should_emit_idle:
                await self._publish_session_status_async(session_id, "idle")
                # 步骤 5（无感）：本轮结束后调度后台空闲压缩。
                if inbound.from_channel != SUBAGENT_CHANNEL_ID:
                    try:
                        await self._schedule_idle_compact(session_id, inbound.from_channel)
                    except Exception:
                        logger.debug("[agent-loop] 调度 idle 压缩失败", exc_info=True)

    # ─── idle 压缩调度 ──────────────────────────────────────

    async def _schedule_idle_compact(self, session_id: str, channel_id: str) -> None:
        """主事件循环里：水位 ≥ threshold → 后台压缩。

        去重：同一 session 同一时间只允许一个后台 compact task 在飞。
        如果上一个还没完成就不再派发，避免 cron session 连续触发导致反复压缩。
        """
        try:
            config = self._load_current_config()
            if not getattr(config.context, "idle_compaction", True):
                return
            need = await self.compact_handler.should_compact(
                session_id,
                channel_id,
                config,
                threshold=getattr(config.context, "precompact_threshold", 0.5),
            )
            if not need:
                return

            # 去重：同一 session 已有后台 compact 在飞则跳过
            if session_id in self._compact_tasks:
                logger.debug(f"[agent-loop] session={session_id} 已有后台压缩在飞，跳过")
                return

            # 后台隐形压缩：直接启用 compact event，让下一轮上下文立刻使用摘要。
            async def _do_compact():
                try:
                    await self.compact_handler.compact(
                        session_id, channel_id,
                        config=config,
                        silent=getattr(config.context, "silent", True),
                        enabled=True,
                    )
                finally:
                    self._compact_tasks.pop(session_id, None)

            task = asyncio.create_task(_do_compact())
            self._compact_tasks[session_id] = task
            logger.info(f"[agent-loop] idle 后台压缩已派发 session={session_id}")
        except Exception:
            logger.exception(f"[agent-loop] idle 压缩调度异常 session={session_id}")

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
    ) -> tuple[str | list[dict], AgentConfig]:
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

        if isinstance(user_content, str):
            return user_content, hook_config
        return [{"role": "user", "content": user_content}], hook_config

    def _create_agent(self, config: AgentConfig) -> ReActAgent:
        """根据配置创建 ReActAgent 实例。"""
        c = config
        tools = build_default_tools(
            channel_manager=self.channel_manager,
            tool_registry=self.tool_registry,
        )

        # MCP 工具提示词注入
        system_prompt = c.system_prompt
        if self.mcp_manager:
            mcp_hint = self.mcp_manager.build_system_hint()
            if mcp_hint:
                system_prompt = system_prompt + mcp_hint

        return ReActAgent(
            model=c.llm.model,
            api_key=c.llm.api_key,
            api_base=c.llm.api_base,
            api_type=c.llm.api_type,
            system_prompt=system_prompt,
            tools=tools,
            max_iterations=c.max_iterations,
        )
