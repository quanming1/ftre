from __future__ import annotations

from cordis import PluginContext

from .service import SkillService
from .tool import build_load_skill_tool

inject = ("tools", "system_prompt")
provide = ("skills",)


def apply(ctx: PluginContext, config=None):
    if ctx.optional("skills") is not None:
        return
    service = SkillService()
    ctx.provide("skills", service)
    disposer = ctx.tools.register(build_load_skill_tool(service), owner="skill", source="builtin")
    ctx.effect(disposer, label="tool:loadSkill")
