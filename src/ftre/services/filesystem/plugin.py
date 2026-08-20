from __future__ import annotations

from cordis import PluginContext

from .local import LocalFilesystemService

provide = ("filesystem",)
inject = ()


def apply(ctx: PluginContext, config=None):
    if ctx.optional("filesystem") is not None:
        return None
    ctx.provide("filesystem", LocalFilesystemService())
