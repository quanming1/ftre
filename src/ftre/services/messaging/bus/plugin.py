"""业务消息总线的 Provider Plugin。

先创建 ``message_bus``，再由 Channel Provider 注入它；inbound 消费者和 Hook
分发也由本 Plugin 拥有，Agent Runtime 不再消费 Bus。
"""

from __future__ import annotations

from cordis import Context

from .service import MessageBusService

provide = ("message_bus",)
inject = ("hook_runtime",)


def apply(ctx: Context, config=None):
    """发布总线门面，供 Channel 和 Agent 数据面注入使用。"""
    if ctx.get("message_bus", strict=False) is not None:
        return
    service = MessageBusService(hooks=ctx.hook_runtime)
    ctx.provide("message_bus", service)
    service.start()
    ctx.effect(lambda: service.close, label="message-bus:close")
