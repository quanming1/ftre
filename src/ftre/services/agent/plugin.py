"""Provider Plugin for the public Agent facade.

The concrete AgentLoop is injected later by the Gateway data-plane bootstrap;
this Plugin only makes the stable ``agents`` Service key available.
"""

from __future__ import annotations

from cordis import PluginContext

from .service import AgentService

inject = ("agent_profiles",)
provide = ("agents",)


def apply(ctx: PluginContext, config=None):
    """Create the facade unless Composition already supplied an instance."""
    if ctx.optional("agents") is not None:
        return
    options = config if isinstance(config, dict) else {}
    ctx.provide("agents", AgentService(options.get("loop"), ctx.agent_profiles))
