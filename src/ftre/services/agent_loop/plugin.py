"""Agent runtime Provider Plugin.

This plugin is the lifecycle owner for the concrete AgentLoop.  It consumes
public Context services, attaches the narrow runtime to ``AgentService`` and
starts the optional Inbox worker without exposing Queue types to the Agent
service itself.
"""

from __future__ import annotations

from cordis import Context

from .driver import AgentLoopDriver
from .provider import AgentLoopProvider

inject = (
    "agents",
    "sessions",
    "message_bus",
    "channels",
    "tools",
    "commands",
    "agent_profiles",
    "hook_runtime",
    "plugin_manager",
    "traces",
)
provide = ("agent_runtime",)


class AgentRuntimeService:
    """Public lifecycle handle for the concrete AgentLoop runtime."""

    key = "agent_runtime"

    def __init__(self, loop, driver, agents) -> None:
        self.loop = loop
        self.driver = driver
        self._agents = agents
        self._started = False
        self._closed = False

    def start(self) -> None:
        """Start the AgentLoop after all Plugin contributions are loaded."""
        if self._started:
            return
        self.loop.start()
        self._started = True

    async def close(self) -> None:
        """Stop the loop, detach the public driver and leave Inbox cleanup to its Plugin."""
        if self._closed:
            return
        if self._started or self.loop.session_event_unbind is not None:
            await self.loop.stop()
            self._started = False
        self._agents.detach_driver()
        self._closed = True


async def apply(ctx: Context, config=None):
    """Build and own one Agent runtime from the Composition Context."""
    existing = ctx.get("agent_runtime", strict=False)
    if existing is not None:
        return

    loop = AgentLoopProvider.from_context(ctx, ctx.plugin_manager).build()
    driver = AgentLoopDriver(loop)
    ctx.agents.attach_driver(driver, ctx.agent_profiles)
    # Inbox is an optional peer Plugin.  It attaches itself after this
    # runtime is provided; the runtime starts with an explicit capability
    # error instead of importing or owning Queue behavior.
    loop.bind_inbox(None)

    service = AgentRuntimeService(loop, driver, ctx.agents)
    ctx.provide("agent_runtime", service)
    # Return the async disposer to Cordis; passing ``service.close`` directly
    # would execute it immediately during effect registration.
    ctx.effect(lambda: service.close, label="agent-runtime:close")


__all__ = ["AgentRuntimeService", "apply", "inject", "provide"]
