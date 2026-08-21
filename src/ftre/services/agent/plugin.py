"""Provider Plugin for the public Agent Registry/Service.

The data-plane Driver is attached later by the independent runtime Provider;
this Plugin only makes the stable ``agents`` Service key available.
"""

from __future__ import annotations

from cordis import Context

from .service import AgentService

inject = ("agent_profiles",)
provide = ("agents",)


def apply(ctx: Context, config=None):
    """Create the facade unless Composition already supplied an instance."""
    if ctx.get("agents", strict=False) is not None:
        return
    ctx.provide("agents", AgentService(ctx.agent_profiles))
