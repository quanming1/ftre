"""Provider Plugin for per-session workspace boundaries."""

from __future__ import annotations

from cordis import PluginContext

from .service import WorkspaceService

inject = ("sessions",)
provide = ("workspaces",)


def apply(ctx: PluginContext, config=None):
    """Publish a workspace facade that resolves roots through SessionService."""
    if ctx.optional("workspaces") is not None:
        return
    ctx.provide("workspaces", WorkspaceService(ctx.sessions))
