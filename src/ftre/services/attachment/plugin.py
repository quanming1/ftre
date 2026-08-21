"""Provider Plugin for request attachment storage."""

from __future__ import annotations

from cordis import Context

from .service import AttachmentService

provide = ("attachments",)
inject = ()


def apply(ctx: Context, config=None):
    """Publish the default attachment Service and let Fiber own its cleanup."""
    if ctx.get("attachments", strict=False) is not None:
        return
    ctx.provide("attachments", AttachmentService())
