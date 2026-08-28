"""ToolDefinition factory that keeps Skill loading inside the Skill Feature boundary."""
# Skill 工具工厂：把技能名称解析委托给 SkillService，保持 Agent Runtime 不知道目录布局。
# 工具执行时按名字取 winner，命中则返回正文，未命中返回错误提示。

from __future__ import annotations

from ftre_agent.tool import ToolDefinition, ToolParameter


def build_load_skill_tool(service):
    """Create a tool whose execution resolves one named Skill."""
    async def load_skill(name: str):
        record = service.get(name)
        if record is None:
            return f"Skill not found: {name}"
        return record.content

    return ToolDefinition(name="loadSkill", description="Load a named Skill", parameters=[ToolParameter("name", "string", "Skill name")], func=load_skill)
