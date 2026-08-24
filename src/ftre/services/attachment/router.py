"""HTTP routes for root-confined attachment images."""
# 中文说明：附件 HTTP 路由：通过 AttachmentService 做 root 校验和读取，不能接受任意本地路径。

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


def build_router(service) -> APIRouter:
    """Build attachment routes using the AttachmentService path policy."""
    router = APIRouter()

    @router.get("/image-file")
    async def serve_image_file(path: str):
        try:
            candidate, mime = service.resolve_local_image(path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="image not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        return FileResponse(candidate, media_type=mime)

    @router.get("/images/{filename}")
    async def serve_image(filename: str):
        try:
            path = service.resolve(filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="图片不存在或已被清理")
        return FileResponse(path)

    return router
