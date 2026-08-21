"""Provider Plugin for ordered, scoped system-prompt sections."""

from __future__ import annotations

from cordis import Context

from .service import SystemPromptService

provide = ("system_prompt",)
inject = ()


def apply(ctx: Context, config=None):
    """Publish the prompt registry; application prompt text comes from Agent config."""
    if ctx.get("system_prompt", strict=False) is not None:
        return
    service = SystemPromptService()
    ctx.provide("system_prompt", service)
