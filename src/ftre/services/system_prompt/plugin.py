"""Provider Plugin for ordered, scoped system-prompt sections."""

from __future__ import annotations

from pathlib import Path

from cordis import PluginContext

from .service import PromptSection, SystemPromptService

provide = ("system_prompt",)
inject = ()


def apply(ctx: PluginContext, config=None):
    """Publish the prompt registry and load the immutable base section."""
    if ctx.optional("system_prompt") is not None:
        return
    service = SystemPromptService()
    ctx.provide("system_prompt", service)
    base = Path(__file__).resolve().parent / "base.md"
    if base.exists():
        service.register_section(PromptSection(name="base", content=base.read_text(encoding="utf-8"), owner="system-prompt"))
