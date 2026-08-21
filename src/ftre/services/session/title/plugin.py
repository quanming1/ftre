"""Session title behavior Plugin.

The generator is a Service consumer and registers its before-messages hook on
the Cordis Context; it is not a legacy Kernel Plugin subclass.
"""

from __future__ import annotations

import asyncio

from cordis import PluginContext
from ftre.services.agent.runtime.hooks import BEFORE_MESSAGES_BUILD
from ftre.services.system_prompt.types import PromptSection

from .generator import TitleGenPlugin

inject = ("sessions", "system_prompt")
provide = ()


def apply(ctx: PluginContext, config=None):
    """Register title guidance and automatic first-turn generation."""
    generator = TitleGenPlugin(ctx.sessions, asyncio.get_running_loop())
    generator.configure(config)
    ctx.on(BEFORE_MESSAGES_BUILD, generator._on_build)
    disposer = ctx.system_prompt.register_section(
        PromptSection(
            name="title-generation",
            content="Session titles are concise and descriptive.",
            priority=90,
            owner="session-title",
            source="builtin",
        )
    )
    ctx.effect(disposer, label="prompt:session-title")
