"""HTTP routes for inspecting MCP Feature state."""
# MCP 诊断路由：只查询公开 McpService，不能直接读取连接池或启动新的 MCP 进程。

from __future__ import annotations

from fastapi import APIRouter


def build_router(service) -> APIRouter:
    """Build routes against the injected McpService, not a global singleton."""
    router = APIRouter(prefix="/mcp")

    # 列出已注册服务器（可选按 scope 过滤：global / agent:<id>）
    @router.get("")
    async def list_servers(scope: str | None = None):
        return {"servers": [state.__dict__ for state in service.list(scope)]}

    return router
