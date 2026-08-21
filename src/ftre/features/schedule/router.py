"""HTTP surface for Schedule jobs; persistence remains behind Service APIs."""

from __future__ import annotations

from croniter import croniter
from fastapi import APIRouter, HTTPException, Request

from .service import ScheduleService


def build_router(service: ScheduleService) -> APIRouter:
    """Build cron routes without reaching into Store or filesystem details."""
    router = APIRouter(prefix="/cron")

    def validate(payload: dict, *, require_all: bool) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
        cleaned: dict = {}
        for field in ("cron", "title", "prompt"):
            value = payload.get(field)
            if value is None:
                if require_all:
                    raise HTTPException(status_code=400, detail=f"缺少字段: {field}")
                continue
            if not isinstance(value, str) or not value.strip():
                raise HTTPException(status_code=400, detail=f"{field} 不能为空")
            value = value.strip()
            if field == "cron" and not croniter.is_valid(value):
                raise HTTPException(status_code=400, detail=f"无效的 cron 表达式: {value}")
            cleaned[field] = value
        if "disabled" in payload:
            if not isinstance(payload["disabled"], bool):
                raise HTTPException(status_code=400, detail="disabled 必须是布尔值")
            cleaned["disabled"] = payload["disabled"]
        if not require_all and not cleaned:
            raise HTTPException(
                status_code=400,
                detail="至少需要更新 cron / title / prompt / disabled 中的一项",
            )
        return cleaned

    @router.get("")
    async def list_jobs():
        return {"jobs": service.list()}

    @router.get("/{job_id}")
    async def get_job(job_id: str):
        try:
            job = service.get(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if job is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
        return job

    @router.post("", status_code=201)
    async def create_job(request: Request):
        payload = await request.json()
        cleaned = validate(payload, require_all=True)
        try:
            return service.create(cleaned)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/{job_id}")
    async def update_job(job_id: str, request: Request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
        illegal = set(payload) - {"cron", "title", "prompt", "disabled"}
        if illegal:
            raise HTTPException(status_code=400, detail=f"不允许修改字段: {sorted(illegal)}")
        cleaned = validate(payload, require_all=False)
        try:
            return service.update(job_id, cleaned)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.delete("/{job_id}", status_code=204)
    async def delete_job(job_id: str):
        try:
            deleted = service.delete(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")

    return router
