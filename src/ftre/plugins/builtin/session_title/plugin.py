"""Session title behavior Plugin.

The generator is a Service consumer and observes structured prompt assembly
through the Cordis Context.
"""
# 中文说明：标题 Plugin Provider：注入 sessions/system_prompt/hook runtime，注册 assemble Hook 并绑定线程停止清理。

from __future__ import annotations

import asyncio

from cordis import Context

from ftre.services.system_prompt.hooks import (
    SYSTEM_PROMPT_ASSEMBLE_SPEC,
    PromptAssemblyPayload,
)
from ftre.services.system_prompt.types import PromptSection

from .generator import TitleGenPlugin

inject = ("sessions", "system_prompt", "hook_runtime")
provide = ()


def apply(ctx: Context, config=None):
    """Register title guidance and automatic first-turn generation."""
    generator = TitleGenPlugin(ctx.sessions, asyncio.get_running_loop())
    generator.configure(config)
    async def on_prompt_assemble(payload: PromptAssemblyPayload, next_):
        """Run title observation without mutating the prompt or message history."""
        await generator._on_build(payload)
        return await next_()

    receipt = ctx.hook_runtime.register(
        SYSTEM_PROMPT_ASSEMBLE_SPEC,
        on_prompt_assemble,
        owner="session-title",
        context=ctx,
        global_listener=True,
    )
    ctx.effect(
        lambda: receipt.dispose,
        label="hook:system-prompt:session-title",
    )
    ctx.effect(lambda: generator.close, label="session-title:close")
    disposer = ctx.system_prompt.register_section(
        PromptSection(
            name="title-generation",
            content="Session titles are concise and descriptive.",
            priority=90,
            owner="session-title",
            source="builtin",
        )
    )
    ctx.effect(lambda: disposer, label="prompt:session-title")
