"""跨 Session 消息 Tool 的独立 Cordis Plugin。

Inbox 只负责队列；本 Plugin 负责 ``send_message`` 的 notify/invoke 业务行为。
invoke 会消费注入的 Inbox，notify 则通过公开 MessageBus 发送，不把这些行为注册回
Inbox Plugin。
"""

from __future__ import annotations

from cordis import Context

from .send_message import create_send_message_tool

inject = ("channels", "tools", "inbox")
provide = ()


async def apply(ctx: Context, config=None):
    """注册 send_message，并把注销动作绑定到当前 Plugin Fiber。"""
    tool = create_send_message_tool(ctx.channels.manager, ctx.inbox)
    disposer = ctx.tools.register(tool, owner="messaging", source="package")
    ctx.effect(lambda: disposer, label="messaging:tool:send_message")
