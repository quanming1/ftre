"""Session Service 的 Provider Plugin（SessionLog + 帧转发装配）。"""

from __future__ import annotations

from cordis import Context

from .service import SessionService

inject = ("hook_runtime", "message_bus")
provide = ("sessions",)


async def apply(ctx: Context, config=None):
    """先初始化 Session 存储（元信息 + 事件日志），再发布 ``sessions`` Service。"""
    service = ctx.get("sessions", strict=False)
    if service is None:
        options = config if isinstance(config, dict) else {}
        service = SessionService(
            sessions_dir=options.get("sessions_dir"),
            hook_runtime=ctx.hook_runtime,
            snapshot_interval_ms=int(options.get("snapshot_interval_ms", 500)),
        )
        # 唯一帧出口：SessionLog 事件 → message_bus.publish_frame（session/event）
        service.set_frame_publisher(ctx.message_bus.publish_frame)
        await service.init()
        ctx.provide("sessions", service)

    # Composition tests and embedders may provide a narrow Session contract
    # instead of the default implementation; only the owned implementation
    # participates in these lifecycle hooks.
    if isinstance(service, SessionService):
        ctx.effect(lambda: service.close, label="sessions:close")
