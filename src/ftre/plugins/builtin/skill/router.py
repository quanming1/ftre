"""HTTP routes for Skill catalog and global Skill CRUD."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException


def build_router(service) -> APIRouter:
    """Build routes against the injected SkillService only."""
    router = APIRouter(prefix="/skills")

    @router.get("")
    async def list_skills(agent_id: str = "default", workspace: str | None = None):
        return {"skills": service.list(agent_id, workspace)}

    @router.get("/diagnostics")
    async def skill_diagnostics(agent_id: str = "default", workspace: str | None = None):
        service.list(agent_id, workspace)
        return {"diagnostics": list(service.diagnostics)}

    @router.get("/{name}")
    async def get_skill(name: str, agent_id: str = "default", workspace: str | None = None):
        item = service.get(name, agent_id, workspace)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Skill 不存在: {name}")
        return service.serialize(item, agent_id)

    @router.post("")
    async def create_skill(payload: dict[str, Any]):
        try:
            item = service.create(
                name=payload.get("name", ""),
                content=payload.get("content", ""),
                description=payload.get("description", ""),
                kind=payload.get("kind", "dir"),
            )
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return service.serialize(item)

    @router.put("/{name}")
    async def update_skill(name: str, payload: dict[str, Any]):
        try:
            item = service.update(name, str(payload.get("content", "")))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return service.serialize(item)

    @router.delete("/{name}", status_code=204)
    async def delete_skill(name: str):
        try:
            service.delete(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/{name}/toggle")
    async def toggle_skill(name: str):
        try:
            return await service.toggle_disabled(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
