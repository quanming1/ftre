from fastapi import APIRouter


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    return router

