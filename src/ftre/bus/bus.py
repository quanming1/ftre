"""
EventBus - 消息网关

- inbound:  全局单队列（AgentLoop 统一消费）
- outbound: 全局单队列（ChannelManager 统一消费，按 to_channel 分发）
"""
import asyncio
import logging
from typing import Callable

from .message import BusMessage, TypedBusMessage

logger = logging.getLogger(__name__)

Middleware = Callable[[BusMessage], BusMessage | None]


class EventBus:

    _TYPED_TOPIC_PREFIXES = ("session_event:", "global_event:")

    def __init__(self):
        self._inbound_queue: asyncio.Queue[BusMessage] = asyncio.Queue()
        self._outbound_queue: asyncio.Queue[BusMessage] = asyncio.Queue()
        self._inbound_middlewares: list[Middleware] = []
        self._outbound_middlewares: list[Middleware] = []
        # request_inbound 的等待者。Bus 只关联一次请求/一次应答，不承载业务状态。
        self._inbound_replies: dict[str, asyncio.Future] = {}
        self._stopped = False

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
        self._validate_message(msg)
        await self._inbound_queue.put(msg)

    async def request_inbound(self, msg: BusMessage):
        """Channel/工具请求 AgentLoop 做 durable admission，并等待其明确回复。

        普通 ``publish_inbound`` 仍适合不需要结果的事件；用户输入必须使用此方法，
        否则进程在内存队列与磁盘提交之间退出时，入口无法得知消息是否真正接纳。
        """
        if self._stopped:
            raise RuntimeError("EventBus 已停止接收 inbound 请求")
        if msg.id in self._inbound_replies:
            raise RuntimeError(f"重复的 inbound request id: {msg.id}")
        future = asyncio.get_running_loop().create_future()
        self._inbound_replies[msg.id] = future
        try:
            filtered = self._apply(msg, self._inbound_middlewares)
            if filtered is None:
                raise RuntimeError("inbound 请求被中间件拒绝")
            self._validate_message(filtered)
            await self._inbound_queue.put(filtered)
            return await future
        finally:
            self._inbound_replies.pop(msg.id, None)

    def resolve_inbound(self, request_id: str, result) -> bool:
        """仅由 AgentLoop 在持久化 admission 结束后调用。"""
        future = self._inbound_replies.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True

    def reject_inbound(self, request_id: str, error: Exception) -> bool:
        future = self._inbound_replies.get(request_id)
        if future is None or future.done():
            return False
        future.set_exception(error)
        return True

    def stop_inbound(self) -> None:
        """停止新请求并唤醒已经在等待 AgentLoop 的调用方。"""
        self._stopped = True
        for future in list(self._inbound_replies.values()):
            if not future.done():
                future.set_exception(RuntimeError("EventBus 已停止"))
        self._inbound_replies.clear()

    async def publish_outbound(self, msg: BusMessage) -> None:
        """Agent Loop → Bus"""
        msg = self._apply(msg, self._outbound_middlewares)
        if msg is None:
            return
        self._validate_message(msg)
        await self._outbound_queue.put(msg)

    @classmethod
    def _validate_message(cls, msg: BusMessage) -> None:
        """复合 session/global Topic 必须携带 Pydantic Payload。"""

        if not isinstance(msg, BusMessage):
            raise TypeError("EventBus 只接受 BusMessage 实例")
        if str(msg.type).startswith(cls._TYPED_TOPIC_PREFIXES) and not isinstance(
            msg, TypedBusMessage
        ):
            raise TypeError(
                f"Topic {msg.type!r} 必须使用 TypedBusMessage，不能使用裸 BusMessage"
            )

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
