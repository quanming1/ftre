"""
TestChannel - 用于测试的 Channel 实现

收集所有 outbound 消息到内存列表，方便断言验证。
"""
from ftre.bus import BusMessage, EventBus
from .base import Channel


class TestChannel(Channel):
    """测试用 Channel，outbound 消息存到 outputs 列表。"""

    def __init__(self, bus: EventBus):
        super().__init__(channel_id="test", name="Test Channel", bus=bus)
        self.outputs: list[BusMessage] = []

    async def send(self, msg: BusMessage) -> None:
        self.outputs.append(msg)

    def clear(self) -> None:
        self.outputs.clear()

    @property
    def events(self) -> list[dict]:
        """提取所有 agent event 的原始数据"""
        return [o.data for o in self.outputs]
