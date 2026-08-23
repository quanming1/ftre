"""Agent runtime Provider Plugin boundary.

The Composition Context is the only dependency graph.  Gateway bootstrap does
not create a second service bag or manually copy Service handles into the loop.
The concrete loop remains an implementation detail behind ``AgentService``.
"""

from __future__ import annotations

from cordis import Context

from ftre.kernel.plugins.manager import PluginManager
from ftre.services.agent_loop.runtime.loop.engine import AgentLoop


class AgentLoopProvider:
    """Create the Agent runtime from public Context services."""

    def __init__(self, ctx: Context, plugin_manager: PluginManager) -> None:
        self.ctx = ctx
        self.plugin_manager = plugin_manager

    @classmethod
    def from_context(
        cls,
        ctx: Context,
        plugin_manager: PluginManager,
    ) -> AgentLoopProvider:
        """Build a provider without introducing an intermediate service DTO."""
        return cls(ctx, plugin_manager)

    def build(self) -> AgentLoop:
        """Construct one Loop using only the Composition Context's public keys."""
        ctx = self.ctx
        agents = ctx.get("agents", strict=False)
        tools = ctx.tools
        kwargs = {
            "bus": ctx.message_bus.bus,
            "session_manager": ctx.sessions,
            "channel_manager": ctx.channels.manager,
            "event_hub": ctx,
            "tool_registry": tools.registry,
            "tool_service": tools,
            "mcp_service": ctx.get("mcp", strict=False),
            "command_service": ctx.commands,
            "plugin_manager": self.plugin_manager,
            "agent_manager": ctx.agent_profiles.manager,
            "agent_service": agents,
            "attachments": ctx.get("attachments", strict=False),
            "agent_registry": agents.registry if agents is not None else None,
            "traces": ctx.get("traces", strict=False),
        }
        system_prompt = ctx.get("system_prompt", strict=False)
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        hook_runtime = ctx.get("hook_runtime", strict=False)
        if hook_runtime is not None:
            kwargs["hook_runtime"] = hook_runtime

        loop = AgentLoop(**kwargs)
        session_events = ctx.get("session_events", strict=False)
        if session_events is not None:
            # The runtime owner must release this binding during shutdown.
            loop.session_event_unbind = session_events.bind(loop.emit_session_event)
        return loop


__all__ = ["AgentLoopProvider"]
