"""FastAPI application factory owned by the Gateway App Host."""
# FastAPI 物化边界：把冻结后的 HttpService 路由注册表变成真正的 FastAPI 应用，
# 并施加面向桌面端开发服务器的 CORS 策略。不在这里启动任何业务任务——
# 业务生命周期由 bootstrap/composition 管理，本模块只产出可交付给
# uvicorn / TestClient 的应用实例。

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 默认 CORS 回环策略：localhost / 127.0.0.1 的任意端口变体（含不带端口的裸地址）。
# 正则只匹配回环地址，绝不放行远程 Origin——这是桌面开发场景的安全底线。
_LOCAL_DESKTOP_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def create_app(http_service, *, cors_origins: list[str] | None = None) -> FastAPI:
    """Materialize the frozen HttpService registry and apply desktop CORS policy."""
    # build_app() 把 HttpService 上已注册的路由（健康检查、WS 路径、各 owner 路由）
    # 物化成 FastAPI 实例；title/version 用于 OpenAPI 文档。
    app = http_service.build_app(title="ftre", version="0.2.4")

    # 桌面端开发服务器使用临时端口（如 localhost:48651），若 CORS 只放行裸
    # localhost Origin，浏览器会因缺少 Access-Control-Allow-Origin 响应头，
    # 把每个本来成功的 API 响应都当成网络失败。因此：
    #   - cors_origins 为 None → 走默认策略：正则放行回环地址的任意端口；
    #   - 显式传入自定义列表 → 精确匹配，不叠加正则（部署时用于收窄白名单）。
    default_origins = cors_origins is None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["http://localhost", "http://127.0.0.1"],
        allow_origin_regex=_LOCAL_DESKTOP_ORIGIN_REGEX if default_origins else None,
        allow_credentials=True,  # 允许携带 Cookie（桌面端会话状态依赖它）
        allow_methods=["*"],  # 放行全部 HTTP 方法（GET/POST/PUT/DELETE...）
        allow_headers=["*"],  # 放行全部请求头（Authorization、Content-Type 等）
    )

    return app
