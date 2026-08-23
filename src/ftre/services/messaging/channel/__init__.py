"""Channel 注册 Service；具体协议位于 ``providers`` 子包。"""

from .names import SUBAGENT_CHANNEL_ID
from .service import ChannelService

__all__ = ["SUBAGENT_CHANNEL_ID", "ChannelService"]
