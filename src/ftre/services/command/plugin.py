"""Provider Plugin for command registration and dispatch."""

from __future__ import annotations

from cordis import Context

from .service import CommandService

provide = ("commands",)
inject = ()


def apply(ctx: Context, config=None):
    """Publish a command facade unless the data plane supplied one already."""
    if ctx.get("commands", strict=False) is not None:
        return
    ctx.provide("commands", CommandService())
