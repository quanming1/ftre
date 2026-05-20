"""
ChannelManager - Channel 注册、生命周期、outbound 分发
"""
import asyncio
import logging

from ftre.bus import EventBus, BusMessage
from .base import Channel

logger = logging.getLogger(__name__)


class ChannelManager:
    """
    管理所有 Channel。
    启动后从 Bus 全局 outbound 队列消费，按 to_channel 分发到对应 Channel。
    """

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._channels: dict[str, Channel] = {}
        self._dispatch_task: asyncio.Task | None = None

    def register(self, channel: Channel) -> None:
        self._channels[channel.channel_id] = channel

    def get(self, channel_id: str) -> Channel | None:
        return self._channels.get(channel_id)

    async def start(self) -> None:
        """启动所有 Channel + 全局分发循环"""
        for ch in self._channels.values():
            await ch.start()
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info(f"[channel-manager] started: {list(self._channels.keys())}")

    async def stop(self) -> None:
        """停止分发 + 所有 Channel"""
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        for ch in self._channels.values():
            await ch.stop()
        logger.info("[channel-manager] stopped")

    async def _dispatch_loop(self) -> None:
        """从 Bus 消费 outbound，按 to_channel 分发"""
        try:
            async for msg in self.bus.subscribe_outbound():
                channel = self._channels.get(msg.to_channel)
                if channel:
                    await channel.send(msg)
                else:
                    logger.warning(f"[channel-manager] 未知 to_channel: {msg.to_channel}")
        except asyncio.CancelledError:
            pass
