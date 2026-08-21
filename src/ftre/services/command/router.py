"""HTTP route contribution for the command registry."""

from __future__ import annotations

from fastapi import APIRouter


def build_router(commands) -> APIRouter:
    """Build the command list route against the injected Service."""
    router = APIRouter()

    @router.get("/commands")
    async def list_commands():
        return {"commands": commands.list()}

    return router
