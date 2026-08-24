"""Trace Service 的 Provider Plugin。

插件启用后才创建 SQLite exporter；Trace API 依赖公开 ``traces``，关闭插件不会
影响 Agent 主数据面，只会失去轨迹查询能力。
"""

from __future__ import annotations

from cordis import Context

from .service import TraceService

provide = ("traces",)
inject = ("http",)


def apply(ctx: Context, config=None):
    """发布 TraceService；exporter 的关闭由 Fiber 生命周期统一负责。"""
    service = ctx.get("traces", strict=False)
    if service is None:
        service = TraceService()
        ctx.provide("traces", service)

    from .router import build_router

    disposer = ctx.http.register_router(build_router(service), owner="traces")
    ctx.effect(lambda: disposer, label="http:traces")
