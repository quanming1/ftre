"""HTTP contribution for revisioned ConfigService updates."""
# 中文说明：Config HTTP 路由：用 ConfigService 的 snapshot/revision 更新配置，冲突时返回显式错误。

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .service import ConfigConflictError, ConfigService


def build_router(service: ConfigService) -> APIRouter:
    """Build config routes against an injected service, not a module singleton."""
    router = APIRouter()

    @router.get("/config")
    async def get_config():
        return service.snapshot().value

    @router.put("/config")
    async def replace_config(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="config 必须是 JSON 对象")
        expected = request.headers.get("if-config-revision")
        try:
            revision = int(expected) if expected is not None else None
            snapshot = await service.replace(body, revision)
        except ConfigConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "revision": snapshot.revision}

    return router
