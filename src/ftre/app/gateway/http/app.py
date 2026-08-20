from __future__ import annotations

from fastapi import FastAPI


def create_app(http_service) -> FastAPI:
    app = http_service.build_app(title="ftre", version="0.2.4")

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    return app

