"""HTTP routes for the MCP catalog and source-scoped configuration writes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

# B008 规避：FastAPI 依赖标记不能在函数签名里内联构造，提到模块级单例。
_BODY = Body(...)


def build_router(service) -> APIRouter:
    """Build a thin transport adapter around one injected McpService."""
    router = APIRouter(prefix="/mcp")

    @router.get("")
    async def list_servers(
        agent_id: str = Query("default"),
        workspace: str | None = Query(None),
        view: str = Query("effective"),
    ):
        try:
            servers = service.catalog(
                agent_id=agent_id or "default",
                workspace=workspace,
                view=view,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "servers": [item.to_public_dict() for item in servers],
            "diagnostics": list(service.diagnostics(workspace=workspace)),
        }

    @router.post("")
    async def create_server(
        payload: dict[str, Any] = _BODY,
        scope: str = Query("global"),
        agent_id: str | None = Query(None),
        workspace: str | None = Query(None),
    ):
        try:
            name = payload.get("name")
            config = {key: value for key, value in payload.items() if key != "name"}
            item = await service.create(
                name=name,
                config=config,
                scope=scope,
                agent_id=agent_id,
                workspace=workspace,
            )
            return item.to_public_dict()
        except (KeyError, TypeError, ValueError, NotADirectoryError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.patch("/{name}")
    async def update_server(
        name: str,
        patch: dict[str, Any] = _BODY,
        scope: str = Query("global"),
        agent_id: str | None = Query(None),
        workspace: str | None = Query(None),
    ):
        try:
            item = await service.update(
                name=name,
                patch=patch,
                scope=scope,
                agent_id=agent_id,
                workspace=workspace,
            )
            return item.to_public_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (TypeError, ValueError, NotADirectoryError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/{name}")
    async def delete_server(
        name: str,
        scope: str = Query("global"),
        agent_id: str | None = Query(None),
        workspace: str | None = Query(None),
    ):
        try:
            await service.delete(
                name=name,
                scope=scope,
                agent_id=agent_id,
                workspace=workspace,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (TypeError, ValueError, NotADirectoryError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True}

    return router
