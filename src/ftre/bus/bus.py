"""
EventBus - 消息网关

- inbound:  全局单队列（AgentLoop 统一消费）
- outbound: 全局单队列（ChannelManager 统一消费，按 to_channel 分发）
"""
import asyncio
import logging
from typing import Callable

from .message import BusMessage

logger = logging.getLogger(__name__)

Middleware = Callable[[BusMessage], BusMessage | None]


class EventBus:

    def __init__(self):
        self._inbound_queue: asyncio.Queue[BusMessage] = asyncio.Queue()
        self._outbound_queue: asyncio.Queue[BusMessage] = asyncio.Queue()
        self._inbound_middlewares: list[Middleware] = []
        self._outbound_middlewares: list[Middleware] = []

    # ============================================================
    # 中间件
    # ============================================================

    def use_inbound(self, middleware: Middleware) -> None:
        self._inbound_middlewares.append(middleware)

    def use_outbound(self, middleware: Middleware) -> None:
        self._outbound_middlewares.append(middleware)

    def _apply(self, msg: BusMessage, middlewares: list[Middleware]) -> BusMessage | None:
        for mw in middlewares:
            msg = mw(msg)
            if msg is None:
                return None
        return msg

    # ============================================================
    # 发布
    # ============================================================

    async def publish_inbound(self, msg: BusMessage) -> None:
        """Channel → Bus"""
        msg = self._apply(msg, self._inbound_middlewares)
        if msg is None:
            return
        await self._inbound_queue.put(msg)

    async def publish_outbound(self, msg: BusMessage) -> None:
        """Agent Loop → Bus"""
        msg = self._apply(msg, self._outbound_middlewares)
        if msg is None:
            return
        await self._outbound_queue.put(msg)

    # ============================================================
    # 订阅
    # ============================================================

    async def subscribe_inbound(self):
        """AgentLoop 消费：全局单队列"""
        while True:
            yield await self._inbound_queue.get()

    async def subscribe_outbound(self):
        """ChannelManager 消费：全局单队列"""
        while True:
            yield await self._outbound_queue.get()
