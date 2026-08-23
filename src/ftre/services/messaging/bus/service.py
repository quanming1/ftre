"""消息总线 Service：进程内业务 EventBus 的稳定门面。

Service 只负责把 Channel/Agent 的发布请求交给拥有队列的 ``EventBus``；它不保存
Session 状态、不执行命令，也不决定消息如何持久化。这样 Bus 可以在组合根替换，
而上层仍依赖稳定的 ``message_bus`` key。
"""

from __future__ import annotations

from .bus import EventBus


class MessageBusService:
    """暴露总线 Service，避免各 Channel 自己创建全局 Bus。"""
    key = "message_bus"

    def __init__(self, bus: EventBus | None = None) -> None:
        self.bus = bus or EventBus()

    async def publish_inbound(self, message) -> None:
        """Publish a fire-and-forget inbound message through the owned Bus."""
        await self.bus.publish_inbound(message)

    async def request_inbound(self, message):
        """Submit an inbound request and wait for the AgentLoop admission ACK."""
        return await self.bus.request_inbound(message)

    def stop_inbound(self) -> None:
        """Close inbound admission during Gateway shutdown."""
        self.bus.stop_inbound()
