"""HTTP routes for persisted Agent profiles."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request


def build_router(profiles) -> APIRouter:
    """Build Agent profile routes from one ``AgentProfileService`` instance."""
    router = APIRouter()

    @router.get("/agents")
    async def list_agents():
        return {"agents": profiles.list()}

    @router.patch("/agents/{agent_id}")
    async def update_agent(agent_id: str, request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON")
        if not any(key in body for key in ("llm", "name", "workspace")):
            raise HTTPException(status_code=400, detail="支持更新的字段: llm, name, workspace")
        try:
            return {"ok": True, "config": profiles.update(agent_id, body)}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"agent '{agent_id}' 不存在") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/agents/{agent_id}/prompts")
    async def get_agent_prompts(agent_id: str):
        try:
            return {"prompts": profiles.list_prompts(agent_id)}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/agents/{agent_id}/prompts/{filename}")
    async def update_agent_prompt(agent_id: str, filename: str, request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("content"), str):
            raise HTTPException(status_code=400, detail="请求体必须包含 content 字段")
        try:
            profiles.update_prompt(agent_id, filename, body["content"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @router.post("/agents")
    async def create_agent(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not body.get("id"):
            raise HTTPException(status_code=400, detail="id 不能为空")
        try:
            config = profiles.create(
                agent_id=body["id"],
                name=body.get("name", ""),
                llm_provider=body.get("provider", ""),
                llm_model=body.get("model", ""),
                workspace=body.get("workspace", ""),
            )
            return {"ok": True, "config": config}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/agents/{agent_id}")
    async def delete_agent(agent_id: str):
        try:
            profiles.delete(agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    return router
