"""Channel 注册 Service 的 Provider Plugin。

它注入 ``message_bus`` 后创建唯一 ``ChannelManager``；WebSocket、Subagent 等
Provider 只贡献具体 Channel，不再各自持有一套分发循环。
"""

from __future__ import annotations

from cordis import Context

from .service import ChannelService

inject = ("message_bus",)
provide = ("channels",)


def apply(ctx: Context, config=None):
    """把 ChannelManager 绑定到消息总线并发布 ``channels`` Service。"""
    service = ctx.get("channels", strict=False)
    if service is None:
        from .manager import ChannelManager

        service = ChannelService(ChannelManager(ctx.message_bus.bus))
        ctx.provide("channels", service)
    # Host may start channels explicitly; the Provider still owns the final
    # stop so Composition.close cannot leave the manager dispatch task alive.
    ctx.effect(lambda: service.stop_all, label="channels:stop")
