"""
ChannelManager - Channel 注册、生命周期、outbound 分发
"""
import asyncio
import logging

from ftre.bus import EventBus, BusMessage, GLOBAL_CHANNEL
from .base import Channel

logger = logging.getLogger(__name__)

WS_CHANNEL_ID = "ws"
MIRROR_TO_WS_CHANNELS = {"cron"}


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
        """从 Bus 消费 outbound，按 to_channel 分发。

        to_channel == GLOBAL_CHANNEL 时为全局广播：分发给所有已注册 Channel，
        由各 Channel 的 send() 自行决定如何扇出给它管理的连接。
        """
        try:
            async for msg in self.bus.subscribe_outbound():
                if msg.to_channel == GLOBAL_CHANNEL:
                    for channel in list(self._channels.values()):
                        await channel.send(msg)
                    continue
                channel = self._channels.get(msg.to_channel)
                if channel:
                    await channel.send(msg)
                    if msg.to_channel in MIRROR_TO_WS_CHANNELS:
                        ws_channel = self._channels.get(WS_CHANNEL_ID)
                        if ws_channel is not None and ws_channel is not channel:
                            await ws_channel.send(msg)
                else:
                    logger.warning(f"[channel-manager] 未知 to_channel: {msg.to_channel}")
        except asyncio.CancelledError:
            pass
