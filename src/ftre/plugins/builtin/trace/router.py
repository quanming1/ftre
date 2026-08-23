"""Read-only HTTP routes for Agent traces."""
# 中文说明：Trace HTTP 路由：只通过 TraceService 查询 SQLite 轨迹，避免 API 依赖表结构。

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException


def build_router(service) -> APIRouter:
    """Build trace routes from the public TraceService."""
    router = APIRouter()

    @router.get("/traces")
    async def list_traces(limit: int = 100, offset: int = 0, session_id: str | None = None):
        page = await asyncio.to_thread(service.list, limit=limit, offset=offset, session_id=session_id)
        return {**page, "path": str(service.store.path)}

    @router.get("/traces/{trace_id}")
    async def read_trace(trace_id: str):
        trace = await asyncio.to_thread(service.get, trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"Trace 不存在: {trace_id}")
        return trace

    @router.get("/traces/{trace_id}/runs/{run_id}")
    async def read_trace_run(trace_id: str, run_id: str):
        run = await asyncio.to_thread(service.get_run, trace_id, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run 不存在: {run_id}")
        return {"run": run}

    return router
