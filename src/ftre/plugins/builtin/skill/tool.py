"""ToolDefinition factory that keeps Skill loading inside the Skill Feature boundary."""
# Skill 工具工厂：把技能名称解析委托给 SkillService，保持 Agent Runtime 不知道目录布局。
# 工具执行时按名字取 winner，命中则返回正文，未命中返回错误提示。

from __future__ import annotations

import logging

from ftre_agent.tool import Injected, ToolDefinition, ToolParameter

logger = logging.getLogger(__name__)


def build_load_skill_tool(service):
    """Create a tool whose execution resolves one named Skill."""
    async def load_skill(
        name: str,
        agent_id: str = Injected("agent_id"),
        workspace=Injected("workspace"),  # noqa: B008 - ToolService context injection
    ):
        resolved_workspace = workspace
        if hasattr(workspace, "aget"):
            resolved_workspace = await workspace.aget()
        elif hasattr(workspace, "get"):
            resolved_workspace = workspace.get()
        record = service.get(
            name,
            str(agent_id or "default"),
            str(resolved_workspace) if resolved_workspace else None,
        )
        if record is None:
            logger.warning(
                "[skill] loadSkill miss name=%r agent_id=%s workspace=%s",
                name,
                agent_id or "default",
                resolved_workspace or "",
            )
            return f"Skill not found: {name}"
        if record.disabled or not record.model_invocable:
            return f"Skill not available for model invocation: {name}"
        return record.content

    return ToolDefinition(
        name="loadSkill",
        description=(
            "Load a Skill by its canonical name from Available skills. "
            "Display titles and ftre://v1/skill/... references are accepted."
        ),
        parameters=[
            ToolParameter(
                "name",
                "string",
                "Canonical Skill name, for example refactor-cleanup-audit",
            )
        ],
        func=load_skill,
    )
