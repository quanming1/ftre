"""Command Service 的 Provider Plugin。

它注入 ``agents``/``sessions``，因为内置命令需要通过公开 Service 修改会话或
恢复 Agent；命令注册和生命周期事件监听都通过 ``ctx.effect`` 绑定，卸载插件时
不会残留命令处理器。
"""

from __future__ import annotations

from cordis import Context

from .builtin import register_builtin_commands
from .service import CommandService

provide = ("commands",)
inject = ("agents", "sessions", "http")


def apply(ctx: Context, config=None):
    """发布 CommandService，并把内置命令注册到当前 Fiber 的生命周期中。"""
    service = ctx.get("commands", strict=False)
    if service is None:
        service = CommandService()
        ctx.provide("commands", service)

    disposers = register_builtin_commands(
        service.runtime,
        agents=ctx.agents,
        sessions=ctx.sessions,
    )
    for index, disposer in enumerate(disposers):
        ctx.effect(lambda disposer=disposer: disposer, label=f"command:builtin:{index}")

    async def persist_command_event(event_type, payload):
        session_id = payload.get("session_id") or ""
        if not session_id:
            return
        await ctx.sessions.append_command_event(
            session_id,
            {"type": event_type, **payload},
        )

    lifecycle_disposer = service.bind_lifecycle(persist_command_event)
    ctx.effect(lambda: lifecycle_disposer, label="command:lifecycle")

    from .router import build_router

    route_disposer = ctx.http.register_router(build_router(service), owner="commands")
    ctx.effect(lambda: route_disposer, label="http:commands")
