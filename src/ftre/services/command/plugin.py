"""Provider Plugin for command registration and dispatch."""

from __future__ import annotations

from cordis import PluginContext

from .service import CommandService

provide = ("commands",)
inject = ()


def apply(ctx: PluginContext, config=None):
    """Publish a command facade unless the data plane supplied one already."""
    if ctx.optional("commands") is not None:
        return
    ctx.provide("commands", CommandService())
