"""Provider Plugin for request attachment storage."""

from __future__ import annotations

from cordis import PluginContext

from .service import AttachmentService

provide = ("attachments",)
inject = ()


def apply(ctx: PluginContext, config=None):
    """Publish the default attachment Service and let Fiber own its cleanup."""
    if ctx.optional("attachments") is not None:
        return
    ctx.provide("attachments", AttachmentService())
