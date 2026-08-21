"""Synthetic third-party audit plugin: public Service contracts only."""

from ftre.services.system_prompt.types import PromptSection

inject = ("tools", "system_prompt")
provide = ()


def apply(ctx, config=None):
    seen = {"tools": len(ctx.tools.snapshot()), "prompt": len(ctx.system_prompt.snapshot())}
    section = PromptSection(
        name="audit-receipt",
        content=f"audit tools={seen['tools']} prompts={seen['prompt']}",
        priority=200,
        owner="synthetic-audit",
        source="external:synthetic-audit",
    )
    disposer = ctx.system_prompt.register_section(section)
    ctx.effect(disposer, label="prompt:synthetic-audit")

