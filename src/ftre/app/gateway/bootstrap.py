"""Gateway startup facade used by the CLI and embedders."""

from __future__ import annotations

import asyncio
from typing import Any

from .composition import build_composition


async def start_gateway(*, config: dict[str, Any] | None = None, plugins_dir=None, initial_services=None):
    composition = await build_composition(config, plugins_dir=plugins_dir, initial_services=initial_services)
    http_service = composition.context.get("http")
    if http_service is not None:
        from .http.app import create_app

        composition.http_app = create_app(http_service)
        http_service.freeze()
    return composition


async def run_gateway(*, config: dict[str, Any] | None = None, plugins_dir=None, initial_services=None):
    composition = await start_gateway(config=config, plugins_dir=plugins_dir, initial_services=initial_services)
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await composition.close()


async def run_gateway_runtime(*, port: int | None = None, host: str | None = None, config: dict[str, Any] | None = None, plugins_dir=None):
    """Start the real Gateway data plane through the new Composition Root.

    The existing AgentLoop and WebSocket protocol are retained as runtime
    providers; they receive public Service handles from Composition and are
    stopped by the returned root cleanup path.
    """
    from ftre_agent_core.hooks import FtreCoreHookManager
    from ftre_agent_core.tool import ToolRegistry
    from ftre.agent.agent_manager import AgentManager
    from ftre.agent.loop import AgentLoop
    from ftre.bus import EventBus
    from ftre.channel import ChannelManager, SubagentChannel, WebSocketChannel
    from ftre.command import CommandManager
    from ftre.command.builtin import register_builtin_commands
    from ftre.config import AGENTS_DIR, load_config_file, load_gateway_address
    from ftre.services.agent import AgentService
    from ftre.services.agent.profile import AgentProfileService
    from ftre.services.command import CommandService
    from ftre.services.config import ConfigService
    from ftre.services.messaging.bus import MessageBusService
    from ftre.services.messaging.channel import ChannelService
    from ftre.services.tools import ToolService
    from ftre.tools.cron import CronScheduler

    config_data = config if config is not None else load_config_file()
    session_manager = None
    agent_loop = None
    cron_scheduler = None
    manager = None
    channel_manager = None
    composition = None
    try:
        from ftre.session import SessionManager

        session_manager = SessionManager()
        await session_manager.init()
        bus = EventBus()
        channel_manager = ChannelManager(bus)
        tool_registry = ToolRegistry()
        command_manager = CommandManager()
        agent_manager = AgentManager(agents_dir=AGENTS_DIR)
        agent_manager.ensure_default()
        config_service = ConfigService(initial=config_data)
        message_bus = MessageBusService(bus)
        channel_service = ChannelService(channel_manager)
        tool_service = ToolService(tool_registry)
        command_service = CommandService(command_manager)
        profile_service = AgentProfileService(agent_manager)
        agent_service = AgentService()
        composition = await start_gateway(
            config=config_data,
            initial_services={
                "config": config_service,
                "sessions": session_manager,
                "message_bus": message_bus,
                "channels": channel_service,
                "tools": tool_service,
                "commands": command_service,
                "agent_profiles": profile_service,
                "agents": agent_service,
            },
            plugins_dir=plugins_dir,
        )
        core_hooks = FtreCoreHookManager()
        config_host, config_port = load_gateway_address()
        gateway_host = host if host is not None else config_host
        gateway_port = port if port is not None else config_port
        agent_loop = AgentLoop(
            bus=bus,
            session_manager=session_manager,
            channel_manager=channel_manager,
            event_hub=composition.context.events,
            core_hook_manager=core_hooks,
            tool_registry=tool_registry,
            command_manager=command_manager,
            plugin_manager=composition.plugins,
            agent_manager=agent_manager,
        )
        agent_service.bind(agent_loop, profile_service)
        register_builtin_commands(command_manager, agent_loop)
        ws_channel = WebSocketChannel(bus, host=gateway_host, port=gateway_port, plugin_manager=composition.plugins)
        channel_manager.register(ws_channel)
        channel_manager.register(SubagentChannel(bus))
        agent_loop.start()
        ws_channel.set_session_projection(agent_loop.session_projection)
        ws_channel.set_session_snapshot_provider(agent_loop)
        await channel_manager.start()
        cron_scheduler = CronScheduler(bus=bus, session_manager=session_manager, channel_manager=channel_manager)
        cron_scheduler.start()
        composition.context.get("http").freeze()
        while True:
            await asyncio.sleep(1)
    finally:
        if cron_scheduler is not None:
            await cron_scheduler.stop()
        if agent_loop is not None:
            await agent_loop.stop()
        if channel_manager is not None:
            await channel_manager.stop()
        if composition is not None:
            await composition.close()
        if session_manager is not None:
            await session_manager.close()
