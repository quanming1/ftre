"""Provider Plugin for per-session workspace boundaries."""

from __future__ import annotations

from cordis import Context

from .service import WorkspaceService

inject = ("sessions",)
provide = ("workspaces",)


def apply(ctx: Context, config=None):
    """Publish a workspace facade that resolves roots through SessionService."""
    if ctx.get("workspaces", strict=False) is not None:
        return
    ctx.provide("workspaces", WorkspaceService(ctx.sessions))
