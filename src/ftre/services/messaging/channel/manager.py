"""ChannelManager：Channel 注册、生命周期和 outbound 分发的唯一 Owner。

Manager 从 Bus 的全局 outbound 队列取消息，再依据 ``to_channel`` 分发；它不参与
Session/Agent 业务决策。停止时先取消分发任务，再停止所有 Channel，保证不会在
Provider 卸载后继续向已关闭的连接发送消息。
"""

import asyncio
import logging

from ftre.services.messaging.bus import GLOBAL_CHANNEL, EventBus

from .base import Channel

logger = logging.getLogger(__name__)

WS_CHANNEL_ID = "ws"


class ChannelManager:
    """管理所有 Channel，并拥有一条 outbound 分发任务。"""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._channels: dict[str, Channel] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._started = False

    def register(self, channel: Channel) -> None:
        """登记 Channel；唯一性由上层 ChannelService 在调用前保证。"""
        self._channels[channel.channel_id] = channel

    def unregister(self, channel_id: str) -> bool:
        """注销一个尚未启动或已自行停止的 Channel。"""
        return self._channels.pop(channel_id, None) is not None

    def get(self, channel_id: str) -> Channel | None:
        """按稳定 channel id 查找已注册协议实现。"""
        return self._channels.get(channel_id)

    async def start(self) -> None:
        """启动所有 Channel + 全局分发循环"""
        if self._dispatch_task is not None and not self._dispatch_task.done():
            return
        for ch in self._channels.values():
            await ch.start()
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        self._started = True
        logger.info(f"[channel-manager] started: {list(self._channels.keys())}")

    async def stop(self) -> None:
        """停止分发 + 所有 Channel"""
        if not self._started:
            return
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None
        for ch in self._channels.values():
            await ch.stop()
        self._started = False
        logger.info("[channel-manager] stopped")

    async def _dispatch_loop(self) -> None:
        """从 Bus 消费 outbound，按 to_channel 分发。

        - ``downstream_frame``：双投——owner channel + ws 观察面（PRD-F41 FR5，
          跨 channel 实时观察）。ws 按 attach 集合扇出，未 attach 自然不发送。
        - ``to_channel == GLOBAL_CHANNEL``：全局广播，分发给所有已注册 Channel。
        """
        try:
            async for msg in self.bus.subscribe_outbound():
                if msg.to_channel == GLOBAL_CHANNEL:
                    for channel in list(self._channels.values()):
                        await channel.send(msg)
                    continue
                channel = self._channels.get(msg.to_channel)
                if msg.type == "downstream_frame":
                    ws_channel = self._channels.get(WS_CHANNEL_ID)
                    if channel is not None:
                        await channel.send(msg)
                    if ws_channel is not None and ws_channel is not channel:
                        await ws_channel.send(msg)
                    continue
                if channel:
                    await channel.send(msg)
                else:
                    logger.warning(
                        f"[channel-manager] 未知 to_channel: {msg.to_channel}"
                    )
        except asyncio.CancelledError:
            pass
