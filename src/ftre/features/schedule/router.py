from __future__ import annotations

from fastapi import APIRouter


def build_router(service) -> APIRouter:
    router = APIRouter(prefix="/cron")

    @router.get("")
    async def list_jobs():
        return {"jobs": service.list()}

    @router.delete("/{job_id}", status_code=204)
    async def delete_job(job_id: str):
        service.delete(job_id)

    return router

