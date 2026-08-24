"""
Subagent Channel - 静默通道，专门承载 task 工具派发的子任务

设计：
- inbound：由 task 工具调用 receive() 投递 user_message 到 Bus
- outbound：静默丢弃（subagent 没有外部实时观察者；完成后的 Msg
  由 TurnExecutor 持久化，前端通过 subagent session 历史展示）

跟 CronChannel 一样是 sink-only：注册到 ChannelManager 让 outbound 分发不报
unknown channel；不真的把任何东西推给外部。
"""
import logging

from ftre.services.messaging.bus import BusMessage, EventBus
from ftre.services.messaging.channel.base import Channel
from ftre.services.messaging.channel.names import SUBAGENT_CHANNEL_ID

logger = logging.getLogger(__name__)


class SubagentChannel(Channel):
    """task 工具专用的静默 channel"""

    def __init__(self, bus: EventBus):
        super().__init__(channel_id=SUBAGENT_CHANNEL_ID, name="Subagent Channel", bus=bus)

    async def send(self, msg: BusMessage) -> None:
        """outbound 静默丢弃；完成后的 Msg 由 TurnExecutor 持久化。"""
        return
