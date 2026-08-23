"""Private Agent runtime construction.

Only ``services.agent.plugin`` calls this function. Keeping construction here
keeps the Agent Provider readable without creating a second public Service or
another lifecycle owner.
"""

from __future__ import annotations

from cordis import Context

from ftre.kernel.plugins.manager import PluginManager

from .engine import AgentLoop


def build_runtime(
    ctx: Context,
    plugin_manager: PluginManager,
    agent_service,
) -> AgentLoop:
    """Construct one private Loop from the Provider's injected Service graph."""
    tools = ctx.tools
    kwargs = {
        "bus": ctx.message_bus.bus,
        "session_manager": ctx.sessions,
        "channel_manager": ctx.channels.manager,
        "event_hub": ctx,
        "tool_registry": tools.registry,
        "tool_service": tools,
        "mcp_service": ctx.get("mcp", strict=False),
        "plugin_manager": plugin_manager,
        "agent_manager": ctx.agent_profiles.manager,
        "agent_service": agent_service,
        "attachments": ctx.get("attachments", strict=False),
        "agent_registry": agent_service.registry,
        "traces": ctx.get("traces", strict=False),
        "system_prompt": ctx.system_prompt,
        "hook_runtime": ctx.hook_runtime,
    }
    loop = AgentLoop(**kwargs)
    session_events = ctx.get("session_events", strict=False)
    if session_events is not None:
        # The Agent Provider owns this bridge and releases it with the runtime.
        loop.session_event_unbind = session_events.bind(loop.emit_session_event)
    return loop


__all__ = ["build_runtime"]
