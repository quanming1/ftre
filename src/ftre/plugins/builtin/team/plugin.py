"""Feature Plugin for multi-agent Team lifecycle state."""
# Team Plugin：创建内存 TeamService 并发布 teams key；
# 成员执行仍通过 AgentService/Inbox 完成，本 Plugin 只持有团队元数据。

from __future__ import annotations

from cordis import Context

from .service import TeamService

inject = ()
provide = ("teams",)


def apply(ctx: Context, config=None):
    """Publish a TeamService backed by the injected Session capability."""
    # 防御：已存在同 key（bootstrap 注入）时跳过，保证单实例
    if ctx.get("teams", strict=False) is not None:
        return
    ctx.provide("teams", TeamService())
