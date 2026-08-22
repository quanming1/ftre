"""Gateway startup facade used by the CLI and embedders."""

from __future__ import annotations

import asyncio
from typing import Any

from .composition import build_composition


async def start_gateway(*, config: dict[str, Any] | None = None, plugins_dir=None, initial_services=None):
    """Build a composition and materialize its HTTP Host for embedders/tests."""
    composition = await build_composition(config, plugins_dir=plugins_dir, initial_services=initial_services)
    http_service = composition.context.get("http")
    if http_service is not None:
        from .http.app import create_app

        composition.http_app = create_app(http_service)
        http_service.freeze()
    return composition


async def run_gateway(*, config: dict[str, Any] | None = None, plugins_dir=None, initial_services=None):
    """Keep an embedded Gateway alive until cancellation, then close it."""
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
    from ftre_agent_core.tool import ToolRegistry

    from ftre.services.agent import AgentService
    from ftre.services.agent.profile import AgentProfileService
    from ftre.services.agent.profile.manager import AgentManager
    from ftre.services.agent_loop import (
        AgentLoopProvider,
        AgentRuntimeServices,
    )
    from ftre.services.command import CommandService
    from ftre.services.config import ConfigService
    from ftre.services.config.loader import load_config_file, load_gateway_address
    from ftre.services.config.paths import AGENTS_DIR
    from ftre.services.messaging.bus import EventBus, MessageBusService
    from ftre.services.messaging.channel import ChannelService
    from ftre.services.messaging.channel.manager import ChannelManager
    from ftre.services.messaging.channel.providers.subagent.channel import (
        SubagentChannel,
    )
    from ftre.services.messaging.channel.providers.websocket.channel import (
        WebSocketChannel,
    )
    from ftre.services.session.events import SessionEventService
    from ftre.services.tools import ToolService

    config_data = config if config is not None else load_config_file()
    session_manager = None
    session_events = SessionEventService()
    agent_loop = None
    agent_service = None
    channel_manager = None
    composition = None
    try:
        from ftre.services.session import SessionService

        session_manager = SessionService()
        await session_manager.init()
        bus = EventBus()
        channel_manager = ChannelManager(bus)
        tool_registry = ToolRegistry()
        agent_manager = AgentManager(agents_dir=AGENTS_DIR)
        agent_manager.ensure_default()
        config_service = ConfigService(initial=config_data)
        message_bus = MessageBusService(bus)
        channel_service = ChannelService(channel_manager)
        tool_service = ToolService(tool_registry)
        command_service = CommandService()
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
                "session_events": session_events,
            },
            plugins_dir=plugins_dir,
        )
        config_host, config_port = load_gateway_address()
        gateway_host = host if host is not None else config_host
        gateway_port = port if port is not None else config_port
        runtime_provider = AgentLoopProvider(
            AgentRuntimeServices(
                sessions=composition.context.get("sessions"),
                message_bus=composition.context.get("message_bus"),
                channels=composition.context.get("channels"),
                tools=composition.context.get("tools"),
                commands=composition.context.get("commands"),
                agent_profiles=composition.context.get("agent_profiles"),
                event_hub=composition.context,
                plugin_manager=composition.plugins,
                agents=composition.context.get("agents"),
                attachments=composition.context.get("attachments"),
                system_prompt=composition.context.get("system_prompt"),
                mcp=composition.context.get("mcp"),
                hook_runtime=composition.context.get("hook_runtime"),
                session_events=composition.context.get("session_events"),
            )
        )
        agent_runtime = runtime_provider.build()
        agent_loop = agent_runtime.loop
        agent_service.attach_driver(agent_runtime.driver, profile_service)
        ws_channel = WebSocketChannel(
            bus,
            host=gateway_host,
            port=gateway_port,
            app=composition.http_app,
            attachment_service=composition.context.get("attachments"),
        )
        channel_manager.register(ws_channel)
        channel_manager.register(SubagentChannel(bus))
        agent_loop.start()
        ws_channel.set_session_projection(agent_loop.session_projection)
        ws_channel.set_session_snapshot_provider(agent_loop)
        await channel_manager.start()
        composition.context.get("http").freeze()
        while True:
            await asyncio.sleep(1)
    finally:
        if agent_loop is not None:
            await agent_loop.stop()
        if agent_service is not None:
            agent_service.detach_driver()
        if channel_manager is not None:
            await channel_manager.stop()
        if composition is not None:
            await composition.close()
        if session_manager is not None:
            await session_manager.close()
