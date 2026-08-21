"""Minimal health route contributed by the Gateway host."""

from fastapi import APIRouter


def build_router() -> APIRouter:
    """Return a standalone router so HttpService can own route registration."""
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    return router
