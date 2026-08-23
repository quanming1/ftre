"""HTTP route contribution for the command registry."""
# 中文说明：Command HTTP 路由：读取 CommandService 的命令摘要，命令执行仍由消息接入边界负责。

from __future__ import annotations

from fastapi import APIRouter


def build_router(commands) -> APIRouter:
    """Build the command list route against the injected Service."""
    router = APIRouter()

    @router.get("/commands")
    async def list_commands():
        return {"commands": commands.list()}

    return router
