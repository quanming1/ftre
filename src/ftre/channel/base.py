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

    async def receive(
        self,
        session_id: str,
        data: dict,
        metadata: dict | None = None,
        *,
        kind: str = "user_input",
    ) -> None:
        """
        接收外部输入 → 投递到 Bus。

        子类的协议解析层（如 WebSocketChannel._on_message）应在拆完帧后
        调用这里，而不是自己构造 BusMessage 直接 publish_inbound，确保
        "外部 → Bus" 的入口唯一可控。

        Args:
            session_id: 目标 session
            data:       payload
            metadata:   附加元数据（如外部协议帧 id 通过 metadata["frame_id"] 携带，
                        AgentLoop echo 时回填到 outbound 帧给前端做占位去重）
            kind:       BusMessage.type，目前可取 "user_input" / "cancel"
        """
        msg = BusMessage(
            type=kind,
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
