"""Schedule Feature Plugin and its complete runtime lifecycle."""
# Schedule Plugin：装配 ScheduleService、CronStore、CronScheduler、Cron Channel 和
# cron 工具，并统一注册清理效果（全部可逆：close/channel/tool/scheduler）。
# 所有资源都绑定 ctx.effect，Fiber unload 时按注册顺序逆序释放。

from __future__ import annotations

from cordis import Context

from .channel import CronChannel
from .scheduler import CronScheduler
from .service import ScheduleService
from .tool import build_cron_tool

inject = ("message_bus", "sessions", "channels", "tools", "http")
provide = ("schedule",)


async def apply(ctx: Context, config=None):
    """Publish Schedule, then register and clean up all owned resources."""
    # 防御：已存在同 key（bootstrap 注入）时跳过，保证单实例
    if ctx.get("schedule", strict=False) is not None:
        return
    options = config if isinstance(config, dict) else {}
    # 1. 发布 ScheduleService（配置可指定持久化根目录）
    service = ScheduleService(options.get("root"))
    ctx.provide("schedule", service)
    ctx.effect(lambda: service.close, label="schedule:close")

    channels = ctx.channels
    tools = ctx.tools
    # 2. 注册 Cron Channel（静默 sink），卸载时摘除
    channel_disposer = channels.register(CronChannel(ctx.message_bus.bus), owner="schedule")
    ctx.effect(lambda: channel_disposer, label="schedule:channel")
    # 3. 注册 cron 工具，卸载时摘除
    tool_disposer = tools.register(
        build_cron_tool(service), owner="schedule", source="builtin"
    )
    ctx.effect(lambda: tool_disposer, label="schedule:tool")
    # 4. 启动调度器（后台扫描循环），卸载时停止
    scheduler = CronScheduler(
        service,
        ctx.sessions,
        ctx.message_bus,
        scan_interval=options.get("scan_interval", 30),
    )
    scheduler.start()
    ctx.effect(lambda: scheduler.stop, label="schedule:scheduler")
    from .router import build_router

    route_disposer = ctx.http.register_router(build_router(service), owner="schedule")
    ctx.effect(lambda: route_disposer, label="http:schedule")
