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
from ftre.bus import BusMessage, EventBus
from ftre.config import AgentConfig, load_config
from ftre.session import SessionManager
from ftre.session.multimodal import build_user_content
from ftre.tools import build_default_tools
from ftre.tools._workspace import WorkspaceAccessor

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
    ):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.hook_manager = hook_manager
        self._injected_config = config
        self._task: asyncio.Task | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._active_agents: dict[str, ReActAgent] = {}

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
        """消费循环：user_input → _run()，cancel → 取消 Agent"""
        try:
            async for msg in self.bus.subscribe_inbound():
                if msg.type == "user_input":
                    asyncio.ensure_future(
                        asyncio.get_event_loop().run_in_executor(None, self._run, msg)
                    )
                elif msg.type == "cancel":
                    sid = msg.from_session or msg.data.get("session_id", "")
                    agent = self._active_agents.get(sid)
                    if agent is not None:
                        agent.cancel_nowait()
                    else:
                        logger.warning(f"[agent-loop] cancel: 未找到活跃 agent (session={sid})")
        except asyncio.CancelledError:
            pass

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

    def _run(self, inbound: BusMessage) -> None:
        """在线程中执行 Agent，事件逐条投递回 Bus。"""
        # Step 1: 入参校验
        content = inbound.data.get("content", "")
        attachments = inbound.data.get("attachments") or []
        if not content and not attachments:
            return

        session_id = inbound.data.get("session_id", "")
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

        # Step 3: 构造 user content + 加载配置
        user_content = build_user_content(content, attachments)
        config = self._load_current_config()

        # Step 4: 加载历史 + hook + 上下文治理
        cfg_ws = (config.workspace or "").strip()
        fallback = (
            os.path.abspath(cfg_ws) if cfg_ws and os.path.isdir(cfg_ws) else os.getcwd()
        )
        messages, hook_config = self._build_messages(
            session_id,
            user_content,
            config,
            inbound_data=inbound.data,
            channel_id=inbound.from_channel,
            workspace=fallback,
        )

        # Step 5: 创建独立 Agent
        agent = self._create_agent(hook_config)
        agent.system_prompt = (
            hook_config.system_prompt
            + f"\n\n[当前上下文] channel_id={inbound.from_channel}, session_id={session_id}"
        )
        self._active_agents[session_id] = agent

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
                fallback_cwd=fallback,
            ),
        }

        try:
            for event in agent.run(messages, runtime_context=runtime_context):
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

    def _load_current_config(self) -> AgentConfig:
        """读取当前生效的配置"""
        if self._injected_config is not None:
            return self._injected_config
        return load_config()

    def _build_messages(
        self,
        session_id: str,
        user_content: str | list[dict],
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

        if events:
            history = SessionManager.to_openai_messages(events)
            history.append({"role": "user", "content": user_content})
            return history, hook_config

        if isinstance(user_content, str):
            return user_content, hook_config
        return [{"role": "user", "content": user_content}], hook_config

    def _get_total_tokens(self, session_id: str) -> int:
        """从 SessionManager 取该 session 的 token 总量"""
        usage = asyncio.run_coroutine_threadsafe(
            self.session_manager.get_token_usage(session_id),
            self._event_loop,
        ).result()
        return int(usage.get("total", 0) or 0)

    def _create_agent(self, config: AgentConfig, tools: list | None = None) -> ReActAgent:
        """根据配置创建 ReActAgent 实例。"""
        c = config
        if tools is None:
            tools = build_default_tools(channel_manager=self.channel_manager)
        return ReActAgent(
            model=c.llm.model,
            api_key=c.llm.api_key,
            api_base=c.llm.api_base,
            api_type=c.llm.api_type,
            system_prompt=c.system_prompt,
            tools=tools,
            max_iterations=c.max_iterations,
        )
