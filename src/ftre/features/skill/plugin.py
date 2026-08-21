"""Feature Plugin for Skill discovery and the ``loadSkill`` tool."""

from __future__ import annotations

from cordis import Context

from .service import SkillService
from .tool import build_load_skill_tool

inject = ("tools", "system_prompt")
provide = ("skills",)


def apply(ctx: Context, config=None):
    """Publish the catalog and register its tool as a reversible contribution."""
    if ctx.get("skills", strict=False) is not None:
        return
    service = SkillService()
    ctx.provide("skills", service)
    disposer = ctx.tools.register(build_load_skill_tool(service), owner="skill", source="builtin")
    ctx.effect(lambda: disposer, label="tool:loadSkill")
