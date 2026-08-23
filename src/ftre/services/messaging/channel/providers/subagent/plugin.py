"""Subagent 静默 Channel 的可选 Provider。

子 Agent 的结果仍写入 Session 历史；这个 Channel 只作为内部路由目标，避免
ChannelManager 把 subagent 消息误判为未知通道。
"""

from __future__ import annotations

from cordis import Context

from .channel import SubagentChannel

inject = ("message_bus", "channels")
provide = ()


def apply(ctx: Context, config=None):
    """注册静默通道，并让当前 Fiber 在卸载时撤销注册。"""
    channel = SubagentChannel(ctx.message_bus.bus)
    disposer = ctx.channels.register(channel, owner="subagent-channel")
    ctx.effect(lambda: disposer, label="channel:subagent")
