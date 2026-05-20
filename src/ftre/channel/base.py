"""
Channel 抽象基类

Channel 负责：
1. 收：从外部接收输入 → BusMessage → bus.publish_inbound()
2. 发：ChannelManager 调 send() 推给外部
"""
from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING

from ftre.bus import BusMessage

if TYPE_CHECKING:
    from ftre.bus import EventBus

logger = logging.getLogger(__name__)


class Channel(ABC):
    """
    Channel 抽象基类

    一个 Channel 实例管理多个 session。
    outbound 分发由 ChannelManager 负责，Channel 只实现 send()。
    """

    def __init__(self, channel_id: str, name: str, bus: "EventBus"):
        self.channel_id = channel_id
        self.name = name
        self.bus = bus

    async def start(self) -> None:
        """启动 Channel（子类可覆盖）"""
        logger.info(f"[channel:{self.channel_id}] {self.name} started")

    async def stop(self) -> None:
        """停止 Channel（子类可覆盖）"""
        logger.info(f"[channel:{self.channel_id}] {self.name} stopped")

    async def receive(self, session_id: str, data: dict, metadata: dict = None) -> None:
        """接收外部输入 → 投递到 Bus"""
        msg = BusMessage(
            type="user_input",
            from_channel=self.channel_id,
            from_session=session_id,
            to_channel=self.channel_id,
            to_session=session_id,
            data=data,
            metadata=metadata or {},
        )
        await self.bus.publish_inbound(msg)

    @abstractmethod
    async def send(self, msg: BusMessage) -> None:
        """推送 outbound 消息给外部（子类实现）"""
        ...
