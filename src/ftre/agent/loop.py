"""
AgentLoop - 全局单例，消费所有 session 的 inbound 消息

职责：
- 从 Bus 全局 inbound 队列消费消息
- 收到 user_input 时，加载历史 → 驱动 ReActAgent → 将事件逐条发布到 outbound
- 收到 cancel 时，通知 Agent 中断执行
"""
import asyncio
import logging

from ftre_agent_core.agent import ReActAgent
from ftre.bus import BusMessage, EventBus
from ftre.config import AgentConfig, DEFAULT_CONFIG
from ftre.session import SessionManager
from ftre.tools import build_default_tools

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    全局单例，消费所有 session 的消息。

    生命周期：
    - start()  → 启动消费协程
    - stop()   → 取消消费协程 + 中断 Agent
    """

    def __init__(self, bus: EventBus, session_manager: SessionManager, channel_manager=None, config: AgentConfig = None):
        self.bus = bus
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.config = config or DEFAULT_CONFIG
        self._task: asyncio.Task | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # session_id → 当前正在执行的 agent（用于取消）
        self._active_agents: dict[str, ReActAgent] = {}

    def start(self) -> None:
        """启动消费循环"""
        self._event_loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self._consume())

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
        """
        消费循环：从 Bus 全局 inbound 队列读取消息。

        - user_input → 在线程池中执行 _run()
        - cancel     → 发送取消信号给对应 session 的 Agent
        """
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

    # 需要持久化的事件类型（小写，和 EventType 枚举值一致）
    PERSISTENT_EVENTS = {
        "message_complete",
        "tool_call",
        "tool_result",
        "tool_cancel_requested",
        "tool_cancelled",
        "tool_timed_out",
        "error",
        "done",
    }

    def _run(self, inbound: BusMessage) -> None:
        """
        在线程中执行 Agent，事件逐条投递回 Bus。
        """
        content = inbound.data.get("content", "")
        if not content:
            return

        session_id = inbound.data.get("session_id", "")
        if not session_id:
            logger.warning("[agent-loop] 收到无 session_id 的消息，忽略")
            return

        # Step 1: 加载该 session 的历史，构建消息
        events = asyncio.run_coroutine_threadsafe(
            self.session_manager.get_messages_by_session(session_id),
            self._event_loop,
        ).result()

        if events:
            history = SessionManager.to_openai_messages(events)
            history.append({"role": "user", "content": content})
            messages = history
        else:
            messages = content

        # 每次都建一个独立 agent，彻底避免跨 session 串扰
        agent = self._create_agent()
        agent.system_prompt = (
            self.config.system_prompt
            + f"\n\n[当前上下文] channel_id={inbound.from_channel}, session_id={session_id}"
        )
        self._active_agents[session_id] = agent

        # Step 2: 存储用户输入
        asyncio.run_coroutine_threadsafe(
            self.session_manager.save_message(session_id, "USER_INPUT", inbound.data),
            self._event_loop,
        ).result()

        # Step 3: 驱动 Agent 执行
        runtime_context = {
            "session_id": session_id,
            "channel_id": inbound.from_channel,
            "event_loop": self._event_loop,
            "bus": self.bus,
            "session_manager": self.session_manager,
        }

        try:
            for event in agent.run(messages, runtime_context=runtime_context):
                # Step 4: 持久事件存储
                if event.get("type") in self.PERSISTENT_EVENTS:
                    asyncio.run_coroutine_threadsafe(
                        self.session_manager.save_message(session_id, event["type"], event.get("data", {})),
                        self._event_loop,
                    ).result()

                # Step 5: 推送给前端
                out = BusMessage(
                    type="agent_event",
                    from_channel=inbound.from_channel,
                    to_channel=inbound.to_channel,
                    from_session=inbound.from_session,
                    to_session=inbound.to_session,
                    data=event,
                )
                asyncio.run_coroutine_threadsafe(self.bus.publish_outbound(out), self._event_loop).result()
        finally:
            # 清理活跃引用，让 GC 回收
            if self._active_agents.get(session_id) is agent:
                self._active_agents.pop(session_id, None)

    def _create_agent(self) -> ReActAgent:
        """根据配置创建 ReActAgent 实例"""
        c = self.config
        return ReActAgent(
            model=c.llm.model,
            api_key=c.llm.api_key,
            api_base=c.llm.api_base,
            api_type=c.llm.api_type,
            system_prompt=c.system_prompt,
            tools=build_default_tools(channel_manager=self.channel_manager),
            max_iterations=c.max_iterations,
        )