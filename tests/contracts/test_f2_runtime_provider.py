from __future__ import annotations

from types import SimpleNamespace

from ftre.services.agent_loop import AgentLoopProvider, provider


def test_runtime_provider_maps_public_services_to_loop(monkeypatch) -> None:
    captured = {}

    class FakeLoop:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(provider, "AgentLoop", FakeLoop)
    class FakeContext:
        sessions = object()
        message_bus = SimpleNamespace(bus="bus")
        channels = SimpleNamespace(manager="channels")
        tools = SimpleNamespace(registry="tools")
        commands = SimpleNamespace(manager="commands")
        agent_profiles = SimpleNamespace(manager="profiles")

        def get(self, key, strict=False):
            return {
                "agents": None,
                "mcp": None,
                "attachments": None,
                "system_prompt": None,
                "hook_runtime": None,
                "session_events": None,
            }.get(key)

    context = FakeContext()
    AgentLoopProvider.from_context(context, "plugins").build()

    assert captured == {
        "bus": "bus",
        "session_manager": context.sessions,
        "channel_manager": "channels",
        "event_hub": context,
        "tool_registry": "tools",
        "tool_service": context.tools,
        "mcp_service": None,
        "command_service": context.commands,
        "plugin_manager": "plugins",
            "agent_manager": "profiles",
            "agent_registry": None,
            "agent_service": None,
            "attachments": None,
            "traces": None,
        }
