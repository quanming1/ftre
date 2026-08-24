"""Session HTTP 路由的独立 Provider Plugin。

Session Service 只拥有持久化、消息历史和 Session 生命周期；本 Plugin 通过显式 Inject
拿到 Agent/Inbox，并注册原有的 `/api/sessions*` 与 `/api/workspaces` 路由。这样路由
不会迫使 Session Provider 通过 `ctx.get` 迟查其他 Service，也能独立卸载和重建。
"""

from __future__ import annotations

from cordis import Context

from ftre.services.session.router import build_router

inject = ("sessions", "agents", "inbox", "http")
provide = ()


def apply(ctx: Context, config=None):
    """注册 Session Router，并把 Router disposer 绑定当前 Plugin Fiber。"""
    disposer = ctx.http.register_router(
        build_router(ctx.sessions, ctx.agents, ctx.inbox),
        owner="sessions",
    )
    ctx.effect(lambda: disposer, label="http:sessions")
