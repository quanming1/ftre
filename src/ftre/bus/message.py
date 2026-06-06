"""
Bus 消息定义
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# 全局事件标记：to_channel / to_session 设为这个硬编码值时，
# 表示这是一条不针对单一 channel/session 的全局广播消息。
# ChannelManager 见到 GLOBAL_CHANNEL 会分发给所有已注册 Channel；
# 各 Channel 的 send() 见到 GLOBAL_SESSION 应扇出给自己管理的所有连接。
GLOBAL_CHANNEL = "*"
GLOBAL_SESSION = "*"


@dataclass
class BusMessage:
    """
    总线消息

    from_channel / from_session：消息来源
    to_channel / to_session：消息目标

    Inbound:  from=Channel, to=Agent
    Outbound: from=Agent, to=Channel
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    type: str = ""
    from_channel: str = ""
    from_session: str = ""
    to_channel: str = ""
    to_session: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
