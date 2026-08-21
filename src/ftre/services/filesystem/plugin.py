"""Provider Plugin for policy-checked local filesystem access."""

from __future__ import annotations

from cordis import PluginContext

from .local import LocalFilesystemService

provide = ("filesystem",)
inject = ()


def apply(ctx: PluginContext, config=None):
    """Publish one filesystem facade so all path checks share one policy."""
    if ctx.optional("filesystem") is not None:
        return
    ctx.provide("filesystem", LocalFilesystemService())
