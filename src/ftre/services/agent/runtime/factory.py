"""Agent runtime construction boundary.

The factory consumes public Service handles and is the only place that maps
those handles to the mature AgentLoop constructor.  This keeps the data-plane
algorithm unchanged while removing that wiring from Gateway bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ftre.services.agent.profile.manager import AgentManager
from ftre.services.agent.runtime.loop.engine import AgentLoop


@dataclass(frozen=True)
class AgentRuntimeServices:
    """Public Service handles required to build one Agent runtime."""

    sessions: Any
    message_bus: Any
    channels: Any
    tools: Any
    commands: Any
    agent_profiles: Any
    event_hub: Any
    core_hook_manager: Any
    plugin_manager: Any


class AgentRuntimeProvider:
    """Translate Service facades into the internal AgentLoop dependency set."""

    def __init__(self, services: AgentRuntimeServices) -> None:
        self.services = services

    def build_loop(self) -> AgentLoop:
        """Build the runtime without making old managers part of the public API."""
        return AgentLoop(
            bus=self.services.message_bus.bus,
            session_manager=self.services.sessions,
            channel_manager=self.services.channels.manager,
            event_hub=self.services.event_hub,
            core_hook_manager=self.services.core_hook_manager,
            tool_registry=self.services.tools.registry,
            command_manager=self.services.commands.manager,
            plugin_manager=self.services.plugin_manager,
            agent_manager=self.services.agent_profiles.manager,
        )


__all__ = ["AgentManager", "AgentRuntimeProvider", "AgentRuntimeServices"]
