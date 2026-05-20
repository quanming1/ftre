"""
AgentLoop - 从 Bus 消费 inbound，驱动 Agent，产出 outbound
"""
import asyncio
import logging

from ftre_agent_core.agent import ReActAgent
from ftre_agent_core.tool import Tool
from ftre.bus import BusMessage, EventBus
from ftre.config import AgentConfig, DEFAULT_CONFIG

logger = logging.getLogger(__name__)


class AgentLoop:
    """每个 session 一个实例，消费 inbound → 驱动 Agent → 产出 outbound。"""

    def __init__(self, session_id: str, bus: EventBus, config: AgentConfig = None, tools: list[Tool] = None):
        self.session_id = session_id
        self.bus = bus
        self.config = config or DEFAULT_CONFIG
        self._tools = tools or []
        self._agent = self._create_agent()
        self._task: asyncio.Task | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._event_loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._agent.cancel_nowait()

    async def _consume(self) -> None:
        """消费循环"""
        try:
            async for msg in self.bus.subscribe_inbound(self.session_id):
                if msg.type == "user_input":
                    await asyncio.get_event_loop().run_in_executor(None, self._run, msg)
                elif msg.type == "cancel":
                    self._agent.cancel_nowait()
        except asyncio.CancelledError:
            pass

    def _run(self, inbound: BusMessage) -> None:
        """在线程中执行 Agent，事件逐条投递回 Bus"""
        content = inbound.data.get("content", "")
        if not content:
            return

        for event in self._agent.run(content):
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
        c = self.config
        return ReActAgent(
            model=c.llm.model,
            api_key=c.llm.api_key,
            api_base=c.llm.api_base,
            api_type=c.llm.api_type,
            system_prompt=c.system_prompt,
            tools=self._tools,
            max_iterations=c.max_iterations,
        )
