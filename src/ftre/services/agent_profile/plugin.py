"""Agent Profile Service 的 Provider Plugin。

它创建唯一的 ``AgentManager``，确保默认 profile 存在，然后把 Manager 包在
``AgentProfileService`` 中发布；其他 Service 只依赖公开 key。
"""

from __future__ import annotations

from cordis import Context

from ftre.services.config.paths import AGENTS_DIR

from .manager import AgentManager
from .service import AgentProfileService

inject = ("http", "sessions", "config")
provide = ("agent_profiles",)


def apply(ctx: Context, config=None):
    """创建 profile Owner 并发布；路径可由插件配置覆盖。"""
    service = ctx.get("agent_profiles", strict=False)
    if service is None:
        options = config if isinstance(config, dict) else {}
        manager = AgentManager(
            agents_dir=options.get("agents_dir", AGENTS_DIR),
            config_service=ctx.config,
        )
        manager.ensure_default()
        service = AgentProfileService(
            manager,
            sessions=ctx.sessions,
            config_service=ctx.config,
        )
        ctx.provide("agent_profiles", service)

    from .router import build_router

    disposer = ctx.http.register_router(build_router(service), owner="agent-profiles")
    ctx.effect(lambda: disposer, label="http:agent-profiles")
