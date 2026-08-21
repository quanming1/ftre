"""Tool factory that keeps Skill loading inside the Skill Feature boundary."""

from __future__ import annotations

from ftre_agent_core.tool import Tool, ToolParameter


def build_load_skill_tool(service):
    """Create a tool whose execution resolves one named Skill."""
    async def load_skill(name: str):
        record = service.get(name)
        if record is None:
            return f"Skill not found: {name}"
        return record.content

    return Tool(name="loadSkill", description="Load a named Skill", parameters=[ToolParameter("name", "string", "Skill name")], func=load_skill)
