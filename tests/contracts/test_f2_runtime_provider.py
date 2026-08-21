from __future__ import annotations

from types import SimpleNamespace

from ftre.services.agent_loop import (
    AgentLoopProvider,
    AgentRuntimeServices,
    provider,
)


def test_runtime_provider_maps_public_services_to_loop(monkeypatch) -> None:
    captured = {}

    class FakeLoop:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(provider, "AgentLoop", FakeLoop)
    services = AgentRuntimeServices(
        sessions=object(),
        message_bus=SimpleNamespace(bus="bus"),
        channels=SimpleNamespace(manager="channels"),
        tools=SimpleNamespace(registry="tools"),
        commands=SimpleNamespace(manager="commands"),
        agent_profiles=SimpleNamespace(manager="profiles"),
        event_hub="events",
        plugin_manager="plugins",
    )

    AgentLoopProvider(services).build()

    assert captured == {
        "bus": "bus",
        "session_manager": services.sessions,
        "channel_manager": "channels",
        "event_hub": "events",
        "tool_registry": "tools",
        "command_service": services.commands,
        "plugin_manager": "plugins",
        "agent_manager": "profiles",
        "agent_registry": None,
    }
