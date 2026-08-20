from __future__ import annotations

from cordis import PluginContext

from .service import WorkspaceService

inject = ("sessions",)
provide = ("workspaces",)


def apply(ctx: PluginContext, config=None):
    if ctx.optional("workspaces") is not None:
        return None
    ctx.provide("workspaces", WorkspaceService(ctx.sessions))
