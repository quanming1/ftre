"""HTTP routes for Skill catalog and source diagnostics."""
# Skill 诊断路由：查询最终技能 winner 与来源；所有加载动作仍由 SkillService 执行，
# 路由层不直接读文件系统。

from __future__ import annotations

from fastapi import APIRouter


def build_router(service) -> APIRouter:
    """Build routes against the SkillService supplied by Composition."""
    router = APIRouter(prefix="/skills")

    # 列出某 agent/工作区视角下的全部技能元数据
    @router.get("")
    async def list_skills(agent_id: str = "default", workspace: str | None = None):
        return {"skills": service.list(agent_id, workspace)}

    # 取单个技能内容（按优先级选 winner）
    @router.get("/{name}")
    async def get_skill(name: str, agent_id: str = "default", workspace: str | None = None):
        item = service.get(name, agent_id, workspace)
        return {"name": name, "content": item.content if item else None}

    return router
