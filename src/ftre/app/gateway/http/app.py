from __future__ import annotations

from fastapi import FastAPI


def create_app(http_service, *, cors_origins: list[str] | None = None) -> FastAPI:
    app = http_service.build_app(title="ftre", version="0.2.4")

    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["http://localhost", "http://127.0.0.1"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app
