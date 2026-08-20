from __future__ import annotations

from cordis import PluginContext
from ftre.services.system_prompt.types import PromptSection
from ftre.tools.plan import create_plan_tool

inject = ("tools", "system_prompt")
provide = ()


def apply(ctx: PluginContext, config=None):
    disposer = ctx.tools.register(create_plan_tool(), owner="plan", source="builtin")
    ctx.effect(disposer, label="tool:plan")
    prompt = PromptSection(
        name="plan-guidance",
        content=(
            "Use the plan tool for multi-step work. Update each step to completed "
            "before ending a plan."
        ),
        priority=60,
        owner="plan",
        source="builtin",
    )
    prompt_disposer = ctx.system_prompt.register_section(prompt)
    ctx.effect(prompt_disposer, label="prompt:plan")
