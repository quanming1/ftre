"""Feature Plugin for Skill discovery and the ``loadSkill`` tool."""
# Skill Plugin：聚合技能来源（全局/Agent/工作区），向 ToolService 注册 loadSkill 工具。
# 目录解析与 winner 决策都在 SkillService 内完成，本文件只做装配与可逆清理。

from __future__ import annotations

from cordis import Context

from .service import SkillService
from .tool import build_load_skill_tool

inject = ("tools", "system_prompt", "http")
provide = ("skills",)


def apply(ctx: Context, config=None):
    """Publish the catalog and register its tool as a reversible contribution."""
    # 防御：已存在同 key（bootstrap 注入）时跳过，保证单实例
    if ctx.get("skills", strict=False) is not None:
        return
    service = SkillService()
    ctx.provide("skills", service)
    # 注册 loadSkill 工具，卸载时通过 disposer 摘除
    disposer = ctx.tools.register(build_load_skill_tool(service), owner="skill", source="builtin")
    ctx.effect(lambda: disposer, label="tool:loadSkill")
    from .router import build_router

    route_disposer = ctx.http.register_router(build_router(service), owner="skill")
    ctx.effect(lambda: route_disposer, label="http:skill")
