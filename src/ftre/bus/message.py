"""
Bus 消息定义
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


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
