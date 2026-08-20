"""The single default Composition Root for ftre's backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cordis import Context

from ftre.config import load_config_file
from ftre.platform.plugin_runtime import PluginManager, PluginManifest


def default_manifests() -> list[PluginManifest]:
    return [
        PluginManifest("config", "ftre.services.config.plugin:apply", "builtin", True, True, description="root configuration"),
        PluginManifest("filesystem", "ftre.services.filesystem.plugin:apply", "builtin", True, True, description="path policy and atomic IO"),
        PluginManifest("http-service", "ftre.services.http.plugin:apply", "builtin", True, True, description="route contribution registry"),
        PluginManifest("system-prompt", "ftre.services.system_prompt.plugin:apply", "builtin", True, True, description="prompt section registry"),
        PluginManifest("message-bus", "ftre.services.messaging.bus.plugin:apply", "builtin", True, True, description="business message plane"),
        PluginManifest("tools", "ftre.services.tools.plugin:apply", "builtin", True, True, description="scoped tool registry"),
        PluginManifest("commands", "ftre.services.command.plugin:apply", "builtin", True, True, description="command registry"),
        PluginManifest("sessions", "ftre.services.session.plugin:apply", "builtin", True, True, description="session persistence facade"),
        PluginManifest("agent-profiles", "ftre.services.agent.profile.plugin:apply", "builtin", True, True, description="agent profile merge"),
        PluginManifest("workspaces", "ftre.services.workspace.plugin:apply", "builtin", True, True, description="workspace boundary"),
        PluginManifest("channels", "ftre.services.messaging.channel.plugin:apply", "builtin", True, True, description="channel registry"),
        PluginManifest("attachments", "ftre.services.attachment.plugin:apply", "builtin", True, True, description="attachment storage"),
        PluginManifest("traces", "ftre.services.observability.trace.plugin:apply", "builtin", True, True, description="trace persistence"),
        PluginManifest("agents", "ftre.services.agent.plugin:apply", "builtin", True, True, description="public agent facade"),
        PluginManifest("skill", "ftre.features.skill.plugin:apply", "builtin", False, True, description="skill catalog and tool"),
        PluginManifest("mcp", "ftre.features.mcp.plugin:apply", "builtin", False, True, description="MCP connection state"),
        PluginManifest("plan", "ftre.features.plan.plugin:apply", "builtin", False, True, description="plan behavior"),
        PluginManifest("team", "ftre.features.team.plugin:apply", "builtin", False, True, description="team lifecycle"),
        PluginManifest("schedule", "ftre.features.schedule.plugin:apply", "builtin", False, True, description="cron persistence"),
        PluginManifest("context-govern", "ftre.features.context_govern.plugin:apply", "builtin", True, True, description="workspace governance"),
        PluginManifest("session-title", "ftre.services.session.title.plugin:apply", "builtin", False, True, description="title behavior"),
    ]


@dataclass
class Composition:
    context: Context
    plugins: PluginManager
    config: dict[str, Any]
    http_app: Any | None = None

    @property
    def diagnostics(self) -> list[dict[str, Any]]:
        return self.plugins.diagnostics()

    async def close(self) -> None:
        await self.plugins.close()


async def build_composition(
    config_data: dict[str, Any] | None = None,
    *,
    plugins_dir=None,
    initial_services: dict[str, Any] | None = None,
) -> Composition:
    config = config_data if config_data is not None else load_config_file()
    context = Context()
    for name, value in (initial_services or {}).items():
        if value is not None:
            context.provide(name, value)
    manager = PluginManager(context, plugins_dir=plugins_dir)
    await manager.load(default_manifests(), config)
    return Composition(context=context, plugins=manager, config=config)
