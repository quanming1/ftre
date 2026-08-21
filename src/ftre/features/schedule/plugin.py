"""Feature Plugin for persisted cron jobs and scheduled messages."""

from __future__ import annotations

from cordis import PluginContext

from .service import ScheduleService

inject = ("message_bus", "sessions", "channels")
provide = ("schedule",)


def apply(ctx: PluginContext, config=None):
    """Publish the schedule Service; the scheduler owns runtime delivery."""
    if ctx.optional("schedule") is not None:
        return
    options = config if isinstance(config, dict) else {}
    ctx.provide("schedule", ScheduleService(options.get("root")))
