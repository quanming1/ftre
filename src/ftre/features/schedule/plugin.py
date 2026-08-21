"""Schedule Feature Plugin and its complete runtime lifecycle."""

from __future__ import annotations

from cordis import Context

from .channel import CronChannel
from .scheduler import CronScheduler
from .service import ScheduleService
from .tool import build_cron_tool

inject = ("message_bus", "sessions", "channels", "tools")
provide = ("schedule",)


async def apply(ctx: Context, config=None):
    """Publish Schedule, then register and clean up all owned resources."""
    if ctx.get("schedule", strict=False) is not None:
        return

    options = config if isinstance(config, dict) else {}
    service = ScheduleService(options.get("root"))
    ctx.provide("schedule", service)
    ctx.effect(lambda: service.close, label="schedule:close")

    channels = ctx.channels
    tools = ctx.tools
    channel_disposer = channels.register(CronChannel(ctx.message_bus.bus), owner="schedule")
    ctx.effect(lambda: channel_disposer, label="schedule:channel")

    tool_disposer = tools.register(
        build_cron_tool(service), owner="schedule", source="builtin"
    )
    ctx.effect(lambda: tool_disposer, label="schedule:tool")

    scheduler = CronScheduler(
        service,
        ctx.sessions,
        ctx.message_bus,
        scan_interval=options.get("scan_interval", 30),
    )
    scheduler.start()
    ctx.effect(lambda: scheduler.stop, label="schedule:scheduler")
