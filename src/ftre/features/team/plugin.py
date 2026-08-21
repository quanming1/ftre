"""Feature Plugin for multi-agent Team lifecycle state."""

from __future__ import annotations

from cordis import Context

from .service import TeamService

inject = ()
provide = ("teams",)


def apply(ctx: Context, config=None):
    """Publish a TeamService backed by the injected Session capability."""
    if ctx.get("teams", strict=False) is not None:
        return
    ctx.provide("teams", TeamService())
