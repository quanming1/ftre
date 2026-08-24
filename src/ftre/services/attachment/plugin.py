"""附件 Service 的 Provider Plugin。

它只发布 ``attachments``；WebSocket/HTTP Provider 通过 Service 使用附件，不在各
协议实现中各自创建上传目录。
"""

from __future__ import annotations

from cordis import Context

from .service import AttachmentService

provide = ("attachments",)
inject = ("http",)


def apply(ctx: Context, config=None):
    """发布默认附件 Service；实例由当前 Fiber 统一管理。"""
    service = ctx.get("attachments", strict=False)
    if service is None:
        service = AttachmentService()
        ctx.provide("attachments", service)

    from .router import build_router

    disposer = ctx.http.register_router(build_router(service), owner="attachments")
    ctx.effect(lambda: disposer, label="http:attachments")
