"""
FastAPI 应用
"""
from fastapi import FastAPI
from .routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="ftre", version="0.2.1")
    app.include_router(router)
    return app
