from __future__ import annotations

from cordis import PluginContext

from .service import HttpService

provide = ("http",)
inject = ()


def apply(ctx: PluginContext, config=None):
    if ctx.optional("http") is not None:
        return
    service = HttpService()
    ctx.provide("http", service)
