"""Provider Plugin for command registration and dispatch."""

from __future__ import annotations

from cordis import Context

from .builtin import register_builtin_commands
from .service import CommandService

provide = ("commands",)
inject = ("agents", "sessions")


def apply(ctx: Context, config=None):
    """Publish commands and register builtins against injected Service Owners."""
    service = ctx.get("commands", strict=False)
    if service is None:
        service = CommandService()
        ctx.provide("commands", service)

    disposers = register_builtin_commands(
        service.runtime,
        agents=ctx.agents,
        sessions=ctx.sessions,
    )
    for index, disposer in enumerate(disposers):
        ctx.effect(lambda disposer=disposer: disposer, label=f"command:builtin:{index}")

    async def persist_command_event(event_type, payload):
        session_id = payload.get("session_id") or ""
        if not session_id:
            return
        await ctx.sessions.append_command_event(
            session_id,
            {"type": event_type, **payload},
        )

    lifecycle_disposer = service.bind_lifecycle(persist_command_event)
    ctx.effect(lambda: lifecycle_disposer, label="command:lifecycle")
