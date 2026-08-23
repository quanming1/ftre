"""Feature Plugin that contributes the planning tool and usage guidance."""
# Plan Plugin：向 ToolService 注册计划工具，并向 SystemPromptService 注册
# 使用说明；卸载时撤销这两项贡献（可逆、幂等）。
# 计划工具本身由 services/tools/builtin/plan.py 提供，这里只做装配接线。

from __future__ import annotations

from cordis import Context

from ftre.services.system_prompt.types import PromptSection
from ftre.services.tools.builtin.plan import create_plan_tool

inject = ("tools", "system_prompt")
provide = ()


def apply(ctx: Context, config=None):
    """Register both behavior contributions and bind their cleanup to the Fiber."""
    # 注册计划工具，disposer 绑到 Fiber 生命周期
    disposer = ctx.tools.register(create_plan_tool(), owner="plan", source="builtin")
    ctx.effect(lambda: disposer, label="tool:plan")
    # 注册使用说明段：告诉模型何时使用 plan 工具
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
    ctx.effect(lambda: prompt_disposer, label="prompt:plan")
