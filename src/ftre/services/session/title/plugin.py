from __future__ import annotations

from cordis import PluginContext
from ftre.services.system_prompt.types import PromptSection

inject = ("system_prompt",)
provide = ()


def apply(ctx: PluginContext, config=None):
    disposer = ctx.system_prompt.register_section(PromptSection(name="title-generation", content="Session titles are concise and descriptive.", priority=90, owner="session-title", source="builtin"))
    ctx.effect(disposer, label="prompt:session-title")

