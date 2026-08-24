"""团队协作 Tool 的独立 Cordis Plugin。

团队关系的持久化 Owner 是公开 SessionService 的 metadata；本 Plugin 只提供团队操作
和成员编排，不再创建第二份内存 TeamService。
"""

from __future__ import annotations

from cordis import Context

from .team import create_team_tools

inject = ("sessions", "agents", "channels", "tools", "inbox", "agent_profiles")
provide = ()


async def apply(ctx: Context, config=None):
    """注册全部 team Tool，并将每个 disposer 绑定到当前 Fiber。"""
    for tool in create_team_tools(ctx.channels.manager, ctx.inbox, ctx.agent_profiles):
        disposer = ctx.tools.register(tool, owner="team", source="package")
        ctx.effect(lambda disposer=disposer, name=tool.name: disposer, label=f"team:tool:{tool.name}")
