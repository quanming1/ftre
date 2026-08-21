"""HTTP routes for inspecting MCP Feature state."""

from __future__ import annotations

from fastapi import APIRouter


def build_router(service) -> APIRouter:
    """Build routes against the injected McpService, not a global singleton."""
    router = APIRouter(prefix="/mcp")

    @router.get("")
    async def list_servers(scope: str | None = None):
        return {"servers": [state.__dict__ for state in service.list(scope)]}

    return router
