"""Runtime 跨边界输入协议事件包。

日志事件模型（13 种，wire 契约）定义在 ``ftre_agent.session.events``；
本包只承载 Runtime 的输入侧协议类（不进 SessionLog）。
"""
from ._event import EventBase, HintBlockEvent, UserConfirmResultEvent

__all__ = [
    "EventBase",
    "HintBlockEvent",
    "UserConfirmResultEvent",
]
