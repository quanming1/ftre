from __future__ import annotations

from cordis import PluginContext

from .service import TeamService

inject = ("sessions",)
provide = ("teams",)


def apply(ctx: PluginContext, config=None):
    if ctx.optional("teams") is not None:
        return None
    ctx.provide("teams", TeamService())
