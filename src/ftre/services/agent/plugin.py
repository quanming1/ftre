from __future__ import annotations

from cordis import PluginContext

from .service import AgentService

inject = ("agent_profiles",)
provide = ("agents",)


def apply(ctx: PluginContext, config=None):
    if ctx.optional("agents") is not None:
        return
    options = config if isinstance(config, dict) else {}
    ctx.provide("agents", AgentService(options.get("loop"), ctx.agent_profiles))
