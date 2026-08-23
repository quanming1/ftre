"""HTTP 路由注册表的 Provider Plugin。

这里创建的是注册表，不是监听 Server；App Host 决定何时构建 FastAPI 和启动
uvicorn，因此 HTTP Service 可以在测试和嵌入场景中单独使用。
"""

from __future__ import annotations

from cordis import Context

from .service import HttpService

provide = ("http",)
inject = ()


def apply(ctx: Context, config=None):
    """发布路由注册表；嵌入式 Host 已提供实例时保留其所有权。"""
    if ctx.get("http", strict=False) is not None:
        service = ctx.get("http")
    else:
        service = HttpService()
        ctx.provide("http", service)
    ctx.effect(lambda: service.register_health(), label="http:health")
