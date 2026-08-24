"""Subagent task Tool 的独立 Cordis Plugin。"""

from __future__ import annotations

from cordis import Context

from .task import create_task_tool

inject = ("channels", "tools", "inbox")
provide = ()


async def apply(ctx: Context, config=None):
    """注册 task，并让 Plugin Fiber 负责注销。"""
    tool = create_task_tool(ctx.channels.manager, ctx.inbox)
    disposer = ctx.tools.register(tool, owner="task", source="package")
    ctx.effect(lambda: disposer, label="task:tool:task")
