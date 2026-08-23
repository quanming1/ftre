"""业务消息总线的 Provider Plugin。

先创建 ``message_bus``，再由 Channel Provider 注入它；Provider 不启动消费者，
消费者的生命周期由 AgentLoop/ChannelManager 各自拥有。
"""

from __future__ import annotations

from cordis import Context

from .service import MessageBusService

provide = ("message_bus",)
inject = ()


def apply(ctx: Context, config=None):
    """发布总线门面，供 Channel 和 Agent 数据面注入使用。"""
    if ctx.get("message_bus", strict=False) is not None:
        return
    service = MessageBusService()
    ctx.provide("message_bus", service)
