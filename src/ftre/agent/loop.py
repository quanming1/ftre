"""
AgentLoop - 全局单例，消费所有 session 的 inbound 消息

职责：
- 从 Bus 全局 inbound 队列消费消息
- 收到 user_input 时，加载历史 → 驱动 ReActAgent → 将事件逐条发布到 outbound
- 收到 cancel 时，通知 Agent 中断执行
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

    生命周期：
    - start()  → 启动消费协程
    - stop()   → 取消消费协程 + 中断 Agent
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
    ):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.hook_manager = hook_manager
        self.tool_registry = tool_registry
        self.command_manager = command_manager
        self._injected_config = config
        self._task: asyncio.Task | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._active_agents: dict[str, ReActAgent] = {}

        self._pipeline = Pipeline("consume")
        self._pipeline.use(self._step_command, name="command")
        self._pipeline.use(self._step_compact, name="compact")
        self._pipeline.use(self._step_run, name="run")

        self.compact_handler = CompactHandler(
            session_manager=self.session_manager,
            channel_manager=self.channel_manager,
            bus=self.bus,
            loop_getter=lambda: self._event_loop,
            threshold=self._initial_context_cfg().threshold,
            consolidation_ratio=self._initial_context_cfg().consolidation_ratio,
            safety_buffer=self._initial_context_cfg().safety_buffer,
        )

        self._register_commands()

    def _initial_context_cfg(self):
        """实例化时读一次 ContextConfig 用于 CompactHandler 默认参数。

        运行时每次压缩仍会读最新 config 决定 silent / idle 开关 / 边界算 budget，
        所以这里只用于设定 CompactHandler 的“默认行为常数”（threshold / ratio /
        safety_buffer），改这些需要重启进程。
        """
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
        # /cancel：直接替换 meta["inbound"]，pipeline 下一步会拿到新 inbound
        self.command_manager.register(
            "/cancel",
            lambda ctx: ctx.meta.update(
                inbound=BusMessage(
                    type="cancel",
                    from_channel=ctx.meta["inbound"].from_channel,
                    from_session=ctx.meta["inbound"].from_session,
                    to_channel=ctx.meta["inbound"].to_channel,
                    to_session=ctx.meta["inbound"].to_session,
                    data={"session_id": ctx.meta["inbound"].from_session},
                )
            ),
            description="取消当前会话执行",
        )
        # /compact：手动压缩当前会话上下文（异步 handler，在线程里执行压缩）
        self.command_manager.register(
            "/compact",
            self._cmd_compact,
            description="压缩当前会话上下文",
        )

    async def _cmd_compact(self, ctx) -> None:
        """/compact 指令：fire-and-forget 派发压缩到后台执行。

        绝不能在此 await 压缩完成：dispatch 跑在唯一的 inbound 消费循环里，
        压缩要派发 subagent 并等它跑完，而 subagent 的 inbound 也要靠这个消费
        循环处理。一旦在这里阻塞等待 → 消费循环卡死 → subagent 永远不被执行
        → 死锁。这里只投递到后台任务，立即返回。

        前端 isBusy 由全局 session_status 事件控制（running ↔ idle）：
        - sendMessage 时前端本地立即置 busy=true
        - 后端 _run_async 的 finally 会发 idle 解除
        但 /compact 是命令短路、不走 _run_async，必须在此手动补发 idle，
        否则前端 loading 转圈永不停止。

        注意：直接用 bus.publish_outbound 而不是 _publish_session_status，
        后者用 run_coroutine_threadsafe(...).result() 只能从线程调用，从
        主循环协程里调会自死锁。
        """
        inbound = ctx.meta["inbound"]
        session_id = inbound.from_session
        channel_id = inbound.from_channel

        async def _emit_status(status: str) -> None:
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

        async def _run_compact() -> None:
            await _emit_status("running")
            try:
                # 手动 /compact：可见、走 subagent（高质量），传 config 启用 head/tail。
                # 用 functools.partial 包装，run_in_executor 才能传 keyword 参数。
                from functools import partial
                config = self._load_current_config()
                fn = partial(
                    self.compact_handler.compact,
                    session_id, channel_id,
                    fast=False, config=config, silent=False,
                )
                await self._event_loop.run_in_executor(None, fn)
            except Exception:
                logger.exception(f"[agent-loop] /compact 执行异常 session={session_id}")
            finally:
                await _emit_status("idle")

        asyncio.ensure_future(_run_compact())

    def start(self) -> None:
        """启动消费循环"""
        self._event_loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self._consume())

    def is_session_running(self, session_id: str) -> bool:
        """该 session 是否有正在跑的 ReActAgent。"""
        return session_id in self._active_agents

    async def stop(self) -> None:
        """停止消费循环并中断所有正在运行的 Agent"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for agent in list(self._active_agents.values()):
            agent.cancel_nowait()
        self._active_agents.clear()

    async def _consume(self) -> None:
        """消费循环：Pipeline 路由 (command / cancel / user_input)。"""
        try:
            async for msg in self.bus.subscribe_inbound():
                try:
                    await self._pipeline.run({"inbound": msg})
                except Exception:
                    # 单条消息处理异常不能拖垮整个 consume 循环，否则后续消息全卡死
                    logger.exception("[agent-loop] pipeline 异常，已丢弃该消息")
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _extract_text_content(content) -> str:
        """从 user_input.content 抽取首段纯文本，兼容字符串与多模态分段数组。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for seg in content:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    return str(seg.get("data", "") or "")
        return ""

    async def _step_command(self, data: dict) -> bool:
        """指令预处理：/ 开头的 user_input 交给 CommandManager（handler 可改 inbound）。"""
        inbound = data["inbound"]
        if inbound.type != "user_input" or self.command_manager is None:
            return True
        text = self._extract_text_content(inbound.data.get("content", ""))
        if text.startswith("/"):
            hit = await self.command_manager.dispatch(text, meta=data)
            logger.info(f"[agent-loop] 指令派发 text={text!r} hit={hit}")
            # 命中指令：标记 command_hit。若 handler 未替换 inbound 类型（仍是
            # user_input，如 /compact），后续阶段据此短路，避免把指令文本当消息跑 LLM。
            # /cancel 会把 inbound 换成 cancel 类型，由 _step_run 的 cancel 分支处理。
            if hit:
                data["command_hit"] = True
        return True

    async def _step_compact(self, data: dict) -> bool:
        """压缩阶段：只判断是否需要自动压缩，把结论写入 data['need_compact']。

        ⚠️ 这里只做轻量判断（读 DB / token），绝不在此执行压缩：本阶段运行在唯一
        的 inbound 消费循环里，而压缩要派发 subagent 并等它跑完——subagent 的
        inbound 也要靠这个消费循环处理。若在此执行/等待压缩 → 消费循环卡死 →
        subagent 永不被执行 → 死锁（曾导致压缩会话空空如也）。

        真正的压缩执行下沉到 _run_async 线程里（此时消费循环已空闲，能正常消费
        subagent）。
        """
        inbound = data["inbound"]
        if inbound.type != "user_input" or data.get("command_hit"):
            return True
        session_id = inbound.data.get("session_id", "") or inbound.from_session
        if not session_id:
            return True
        config = self._load_current_config()
        try:
            data["need_compact"] = await self.compact_handler.should_compact(
                session_id, inbound.from_channel, config
            )
        except Exception:
            logger.exception(f"[agent-loop] 压缩判断异常 session={session_id}")
            data["need_compact"] = False
        return True

    def _step_run(self, data: dict) -> bool:
        """按最终 inbound 类型派发。"""
        inbound = data["inbound"]
        if inbound.type == "cancel":
            sid = inbound.from_session or inbound.data.get("session_id", "")
            agent = self._active_agents.get(sid)
            if agent:
                agent.cancel_nowait()
        elif inbound.type == "user_input" and not data.get("command_hit"):
            # fire-and-forget 到线程：消费循环立即回到空闲，subagent 才能被消费。
            # need_compact 透传给 _run，让压缩在这个空闲窗口里安全执行。
            need_compact = bool(data.get("need_compact"))
            asyncio.ensure_future(
                self._event_loop.run_in_executor(
                    None, self._run, inbound, need_compact
                )
            )
        return False

    # 需要持久化的事件类型
    PERSISTENT_EVENTS = {
        "message_complete",
        "reasoning_complete",
        "tool_call",
        "tool_result",
        "tool_cancel_requested",
        "tool_cancelled",
        "tool_timed_out",
        "usage_update",
        "error",
        "done",
    }

    def _run(self, inbound: BusMessage, need_compact: bool = False) -> None:
        """在线程中执行 Agent（sync wrapper → async）。"""
        asyncio.run(self._run_async(inbound, need_compact))

    async def _run_async(self, inbound: BusMessage, need_compact: bool = False) -> None:
        """异步执行 Agent，事件逐条投递回 Bus。"""
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
        session = asyncio.run_coroutine_threadsafe(
            self.session_manager.get_session(session_id),
            self._event_loop,
        ).result()
        if session is None:
            logger.warning(f"[agent-loop] session 不存在，拒绝执行: session={session_id}")
            return
        if session["channel_id"] != inbound.from_channel:
            logger.warning(
                f"[agent-loop] session 与 channel 不匹配: "
                f"session={session_id} (channel={session['channel_id']}), 消息来自 {inbound.from_channel}"
            )
            return

        # Step 2.5: 并发防御 — 同一 session 已在运行时静默丢弃本次执行，
        # 避免 _active_agents[sid] 被覆盖、上下文错乱。
        if self.is_session_running(session_id):
            logger.warning(
                f"[agent-loop] session 正在运行，静默丢弃并发消息: session={session_id}"
            )
            return

        # Step 2.8: 关键路径压缩（用户正等着回复，走 fast=True 本地 raw 兜底）。
        # 这里是安全窗口：_step_run 已 fire-and-forget 派发本协程并让消费循环回到
        # 空闲，压缩派发的 subagent 能被主消费循环正常处理，不会死锁。
        # 压缩把 context_compact 事件写入历史，紧接着的 _build_messages 会读到它。
        #
        # ⚠️ 关键路径绝不在此派慢 subagent 卡住用户——高质量 subagent 压缩交给
        # 每轮结束后的后台空闲压缩（见 finally 的 _schedule_idle_compact）。
        # 传入 config 启用 head/tail 边界切分（保留最近若干轮原文）；silent=True
        # 让前端不渲染气泡，对用户完全无感。
        config = self._load_current_config()
        if need_compact:
            try:
                self.compact_handler.compact(
                    session_id, inbound.from_channel,
                    fast=True, config=config,
                    silent=getattr(config.context, "silent", True),
                )
            except Exception:
                logger.exception(f"[agent-loop] 关键路径压缩失败 session={session_id}")

        # Step 4: 加载历史 + hook + 上下文治理
        # 工作区优先级：session 字段 > config 默认值 > 当前目录
        session_ws = (session.get("workspace") or "").strip()
        if session_ws and os.path.isdir(session_ws):
            workspace = os.path.abspath(session_ws)
        else:
            cfg_ws = (config.workspace or "").strip()
            workspace = os.path.abspath(cfg_ws) if cfg_ws and os.path.isdir(cfg_ws) else os.getcwd()
        messages, hook_config = self._build_messages(
            session_id,
            content,
            attachments,
            config,
            inbound_data=inbound.data,
            channel_id=inbound.from_channel,
            workspace=workspace,
        )

        # Step 5: 创建独立 Agent
        agent = self._create_agent(hook_config)
        agent.system_prompt = (
            hook_config.system_prompt
            + f"\n\n[当前上下文] channel_id={inbound.from_channel}, session_id={session_id}"
        )
        self._active_agents[session_id] = agent
        self._publish_session_status(session_id, "running")

        # Step 6: 持久化用户输入
        asyncio.run_coroutine_threadsafe(
            self.session_manager.save_message(session_id, "USER_INPUT", inbound.data),
            self._event_loop,
        ).result()

        # Step 6.5: echo user_input 给前端
        echo = BusMessage(
            type="agent_event",
            from_channel=inbound.from_channel,
            to_channel=inbound.to_channel,
            from_session=inbound.from_session,
            to_session=inbound.to_session,
            data={"type": "user_input", "data": inbound.data},
            metadata=inbound.metadata,
        )
        asyncio.run_coroutine_threadsafe(
            self.bus.publish_outbound(echo), self._event_loop
        ).result()

        # Step 7: 驱动 Agent 执行
        runtime_context = {
            "session_id": session_id,
            "channel_id": inbound.from_channel,
            "event_loop": self._event_loop,
            "bus": self.bus,
            "session_manager": self.session_manager,
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
                if event.get("type") in self.PERSISTENT_EVENTS:
                    asyncio.run_coroutine_threadsafe(
                        self.session_manager.save_message(session_id, event["type"], event.get("data", {})),
                        self._event_loop,
                    ).result()

                out = BusMessage(
                    type="agent_event",
                    from_channel=inbound.from_channel,
                    to_channel=inbound.to_channel,
                    from_session=inbound.from_session,
                    to_session=inbound.to_session,
                    data=event,
                )
                asyncio.run_coroutine_threadsafe(self.bus.publish_outbound(out), self._event_loop).result()
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
            asyncio.run_coroutine_threadsafe(self.bus.publish_outbound(err_evt), self._event_loop).result()
        finally:
            if self._active_agents.get(session_id) is agent:
                self._active_agents.pop(session_id, None)
                self._publish_session_status(session_id, "idle")
                # 步骤 5（无感）：本轮结束后调度后台空闲压缩。
                # 提交回主事件循环（不是当前 _run_async 的临时循环——它马上要关），
                # 由主循环 _schedule_idle_compact 判水位 + 派 subagent 慢摘高质量。
                # 用户下次发消息时上下文已压好，零等待 → 实现"无感"。
                if inbound.from_channel != SUBAGENT_CHANNEL_ID:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._schedule_idle_compact(session_id, inbound.from_channel),
                            self._event_loop,
                        )
                    except Exception:
                        logger.debug("[agent-loop] 调度 idle 压缩失败", exc_info=True)

    async def _schedule_idle_compact(self, session_id: str, channel_id: str) -> None:
        """主事件循环里：判水位 → 需要时 fire-and-forget 到 executor 跑后台压缩。

        ⚠️ 必须由主循环跑，不能在 _run_async 的临时事件循环里 ensure_future——那个
        循环 finally 后即关闭，任务会被取消（文档 5.2 节陷阱）。
        ⚠️ should_compact 是只读 DB 的轻量操作，绝不在此派 subagent；派发由 executor
        线程里的 compact() 完成（与现有 _step_compact / _run_async 同款规避手法）。
        """
        try:
            config = self._load_current_config()
            if not getattr(config.context, "idle_compaction", True):
                return  # 用户禁用了后台空闲压缩
            need = await self.compact_handler.should_compact(session_id, channel_id, config)
            if not need:
                return
            # 后台压缩：subagent 高质量、silent 由 config 决定，提交到默认 executor。
            from functools import partial
            fn = partial(
                self.compact_handler.compact,
                session_id, channel_id,
                fast=False,
                config=config,
                silent=getattr(config.context, "silent", True),
            )
            self._event_loop.run_in_executor(None, fn)
            logger.info(f"[agent-loop] idle 后台压缩已派发 session={session_id}")
        except Exception:
            logger.exception(f"[agent-loop] idle 压缩调度异常 session={session_id}")

    def _publish_session_status(self, session_id: str, status: str) -> None:
        """广播 session 运行态变化（全局事件，扇出给所有连接）。

        消费者是会话列表等全局视图，它们不一定 attach 了该 session，
        所以走 GLOBAL_CHANNEL / GLOBAL_SESSION 广播而非 per-session 推送。
        status: "running" | "idle"
        """
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
        asyncio.run_coroutine_threadsafe(
            self.bus.publish_outbound(evt), self._event_loop
        ).result()

    def _load_current_config(self) -> AgentConfig:
        """读取当前生效的配置"""
        if self._injected_config is not None:
            return self._injected_config
        return load_config()

    def _build_messages(
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
        events = asyncio.run_coroutine_threadsafe(
            self.session_manager.get_messages_by_session(session_id),
            self._event_loop,
        ).result()

        # 触发 before_messages_build hook（插件做孤立事件清理、相邻性修复、裁剪、标题生成等）
        hook_config = copy.deepcopy(config)
        if self.hook_manager is not None:
            from ftre.plugin import MessagesBuildContext, BEFORE_MESSAGES_BUILD
            ctx = MessagesBuildContext(
                session_id=session_id,
                channel_id=channel_id,
                inbound_data=inbound_data or {},
                workspace=workspace,
                event_loop=self._event_loop,
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
            history = SessionManager.to_openai_messages(
                events,
                config={"llm": {"vision": hook_config.llm.vision}},
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
        return ReActAgent(
            model=c.llm.model,
            api_key=c.llm.api_key,
            api_base=c.llm.api_base,
            api_type=c.llm.api_type,
            system_prompt=c.system_prompt,
            tools=tools,
            max_iterations=c.max_iterations,
        )
