"""
AgentLoop - 从 Bus 消费 inbound，驱动 Agent，产出 outbound

职责：
- 订阅 Bus 的 inbound 队列，按 session 消费消息
- 收到 user_input 时，加载历史 → 驱动 ReActAgent → 将事件逐条发布到 outbound
- 收到 cancel 时，通知 Agent 中断执行
"""
import asyncio
import logging

from ftre_agent_core.agent import ReActAgent
from ftre.bus import BusMessage, EventBus
from ftre.config import AgentConfig, DEFAULT_CONFIG
from ftre.session import SessionManager
from ftre.tools import get_default_tools

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    每个 session 一个实例，消费 inbound → 驱动 Agent → 产出 outbound。

    生命周期：
    - start()  → 启动消费协程
    - stop()   → 取消消费协程 + 中断 Agent
    """

    def __init__(self, session_id: str, bus: EventBus, session_manager: SessionManager, config: AgentConfig = None):
        self.session_id = session_id
        self.bus = bus
        self.session_manager = session_manager
        self.config = config or DEFAULT_CONFIG
        self._agent = self._create_agent()
        self._task: asyncio.Task | None = None          # 消费循环的 asyncio Task
        self._event_loop: asyncio.AbstractEventLoop | None = None  # 主事件循环引用（线程中回调用）

    def start(self) -> None:
        """启动消费循环"""
        self._event_loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        """停止消费循环并中断 Agent"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._agent.cancel_nowait()

    async def _consume(self) -> None:
        """
        消费循环：持续从 Bus inbound 队列读取消息。

        - user_input → 在线程池中执行 _run()（Agent 是同步的，不能阻塞事件循环）
        - cancel     → 发送取消信号给 Agent
        """
        try:
            async for msg in self.bus.subscribe_inbound(self.session_id):
                if msg.type == "user_input":
                    # Agent 执行是同步阻塞的，放到线程池中跑
                    self._run_task = asyncio.ensure_future(
                        asyncio.get_event_loop().run_in_executor(None, self._run, msg)
                    )
                elif msg.type == "cancel":
                    # 通知 Agent 中断当前执行
                    self._agent.cancel_nowait()
        except asyncio.CancelledError:
            pass

    # 需要持久化的事件类型（临时事件如 MESSAGE / REASONING / TOOL_CALL_STREAMING 不存）
    PERSISTENT_EVENTS = {
        "MESSAGE_COMPLETE",
        "TOOL_CALL",
        "TOOL_RESULT",
        "TOOL_CANCEL_REQUESTED",
        "TOOL_CANCELLED",
        "TOOL_TIMED_OUT",
        "ERROR",
        "DONE",
    }

    def _run(self, inbound: BusMessage) -> None:
        """
        在线程中执行 Agent，事件逐条投递回 Bus。
        """
        content = inbound.data.get("content", "")
        if not content:
            return

        session_id = inbound.data.get("session_id", "")

        # Step 1: 构建消息 —— 有 session_id 则加载历史，否则直接用文本
        if session_id:
            # Step 1.1: 从 SQLite 加载该 session 的历史事件
            events = asyncio.run_coroutine_threadsafe(
                self.session_manager.get_messages_by_session(session_id),
                self._event_loop,
            ).result()
            # Step 1.2: 事件流 → OpenAI messages 格式
            history = SessionManager.to_openai_messages(events)
            # Step 1.3: 追加当前用户输入
            history.append({"role": "user", "content": content})
            messages = history
        else:
            messages = content

        # Step 2: 存储用户输入消息
        if session_id:
            asyncio.run_coroutine_threadsafe(
                self.session_manager.save_message(session_id, "USER_INPUT", inbound.data),
                self._event_loop,
            ).result()

        # Step 3: 驱动 Agent 执行
        for event in self._agent.run(messages):
            # Step 4: 持久事件存储到 SQLite
            if session_id and event.get("type") in self.PERSISTENT_EVENTS:
                asyncio.run_coroutine_threadsafe(
                    self.session_manager.save_message(session_id, event["type"], event.get("data", {})),
                    self._event_loop,
                ).result()

            # Step 5: 所有事件（含临时）都推送给前端
            out = BusMessage(
                type="agent_event",
                from_channel="agent",
                from_session=self.session_id,
                to_channel=inbound.from_channel,
                to_session=inbound.from_session,
                data=event,
            )
            asyncio.run_coroutine_threadsafe(self.bus.publish_outbound(out), self._event_loop).result()

    def _create_agent(self) -> ReActAgent:
        """根据配置创建 ReActAgent 实例"""
        c = self.config
        return ReActAgent(
            model=c.llm.model,
            api_key=c.llm.api_key,
            api_base=c.llm.api_base,
            api_type=c.llm.api_type,
            system_prompt=c.system_prompt,
            tools=get_default_tools(),
            max_iterations=c.max_iterations,
        )
