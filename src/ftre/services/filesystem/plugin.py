"""Provider Plugin for policy-checked local filesystem access."""

from __future__ import annotations

from cordis import Context

from .local import LocalFilesystemService

provide = ("filesystem",)
inject = ()


def apply(ctx: Context, config=None):
    """Publish one filesystem facade so all path checks share one policy."""
    if ctx.get("filesystem", strict=False) is not None:
        return
    ctx.provide("filesystem", LocalFilesystemService())
