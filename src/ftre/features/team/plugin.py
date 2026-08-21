"""Feature Plugin for multi-agent Team lifecycle state."""

from __future__ import annotations

from cordis import PluginContext

from .service import TeamService

inject = ("sessions",)
provide = ("teams",)


def apply(ctx: PluginContext, config=None):
    """Publish a TeamService backed by the injected Session capability."""
    if ctx.optional("teams") is not None:
        return
    ctx.provide("teams", TeamService())
