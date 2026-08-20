from __future__ import annotations

from cordis import PluginContext

from .service import CommandService

provide = ("commands",)
inject = ()


def apply(ctx: PluginContext, config=None):
    if ctx.optional("commands") is not None:
        return None
    ctx.provide("commands", CommandService())
