from __future__ import annotations

from types import SimpleNamespace

from ftre.services.agent.runtime import factory
from ftre.services.agent.runtime.factory import (
    AgentRuntimeProvider,
    AgentRuntimeServices,
)


def test_runtime_provider_maps_public_services_to_loop(monkeypatch) -> None:
    captured = {}

    class FakeLoop:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(factory, "AgentLoop", FakeLoop)
    services = AgentRuntimeServices(
        sessions=object(),
        message_bus=SimpleNamespace(bus="bus"),
        channels=SimpleNamespace(manager="channels"),
        tools=SimpleNamespace(registry="tools"),
        commands=SimpleNamespace(manager="commands"),
        agent_profiles=SimpleNamespace(manager="profiles"),
        event_hub="events",
        core_hook_manager="hooks",
        plugin_manager="plugins",
    )

    AgentRuntimeProvider(services).build_loop()

    assert captured == {
        "bus": "bus",
        "session_manager": services.sessions,
        "channel_manager": "channels",
        "event_hub": "events",
        "core_hook_manager": "hooks",
        "tool_registry": "tools",
        "command_manager": "commands",
        "plugin_manager": "plugins",
        "agent_manager": "profiles",
    }
