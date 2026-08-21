"""HTTP routes for Skill catalog and source diagnostics."""

from __future__ import annotations

from fastapi import APIRouter


def build_router(service) -> APIRouter:
    """Build routes against the SkillService supplied by Composition."""
    router = APIRouter(prefix="/skills")

    @router.get("")
    async def list_skills(agent_id: str = "default", workspace: str | None = None):
        return {"skills": service.list(agent_id, workspace)}

    @router.get("/{name}")
    async def get_skill(name: str, agent_id: str = "default", workspace: str | None = None):
        item = service.get(name, agent_id, workspace)
        return {"name": name, "content": item.content if item else None}

    return router
