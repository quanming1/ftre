"""HTTP routes for persisted Schedule jobs."""

from __future__ import annotations

import json
import time
import uuid

from croniter import croniter
from fastapi import APIRouter, HTTPException, Request


def build_router(service) -> APIRouter:
    """Build schedule routes against one Feature-owned service instance."""
    router = APIRouter(prefix="/cron")

    def validate(payload: dict, *, require_all: bool) -> tuple[dict, str | None]:
        cleaned: dict = {}
        for field in ("cron", "title", "prompt"):
            value = payload.get(field)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    return {}, f"{field} 不能为空"
                if field == "cron" and not croniter.is_valid(value.strip()):
                    return {}, f"无效的 cron 表达式: {value}"
                cleaned[field] = value.strip()
            elif require_all:
                return {}, f"缺少字段: {field}"
        if "disabled" in payload:
            if not isinstance(payload["disabled"], bool):
                return {}, "disabled 必须是布尔值"
            cleaned["disabled"] = payload["disabled"]
        if not require_all and not cleaned:
            return {}, "至少需要更新 cron / title / prompt / disabled 中的一项"
        return cleaned, None

    @router.get("")
    async def list_jobs():
        return {"jobs": service.list()}

    @router.get("/{job_id}")
    async def get_job(job_id: str):
        path = service.root / f"{job_id}.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail=f"读取失败: {exc}") from exc

    @router.post("", status_code=201)
    async def create_job(request: Request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
        cleaned, error = validate(payload, require_all=True)
        if error:
            raise HTTPException(status_code=400, detail=error)
        job = {
            "id": f"job_{uuid.uuid4().hex[:10]}",
            **cleaned,
            "disabled": bool(cleaned.get("disabled", False)),
            "created_at": time.time(),
            "run_history": [],
        }
        service.save(job)
        return job

    @router.patch("/{job_id}")
    async def update_job(job_id: str, request: Request):
        path = service.root / f"{job_id}.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
        illegal = set(payload) - {"cron", "title", "prompt", "disabled"}
        if illegal:
            raise HTTPException(status_code=400, detail=f"不允许修改字段: {sorted(illegal)}")
        cleaned, error = validate(payload, require_all=False)
        if error:
            raise HTTPException(status_code=400, detail=error)
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail=f"读取失败: {exc}") from exc
        job.update(cleaned)
        service.save(job)
        return job

    @router.delete("/{job_id}", status_code=204)
    async def delete_job(job_id: str):
        if not service.delete(job_id):
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")

    return router
