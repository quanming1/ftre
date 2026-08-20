from __future__ import annotations

from cordis import PluginContext

from .service import AttachmentService

provide = ("attachments",)
inject = ()


def apply(ctx: PluginContext, config=None):
    if ctx.optional("attachments") is not None:
        return None
    ctx.provide("attachments", AttachmentService())
