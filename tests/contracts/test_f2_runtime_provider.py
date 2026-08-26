from __future__ import annotations

from types import SimpleNamespace

from ftre.services.agent.runtime import provider
from ftre.services.agent.runtime.provider import build_runtime


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
        agent_profiles = SimpleNamespace(manager="profiles")
        system_prompt = "prompt"
        hook_runtime = "hooks"
        session_events = None
        llm = object()

        def get(self, key, strict=False):
            return {
                "mcp": None,
                "attachments": None,
                "session_events": None,
            }.get(key)

    context = FakeContext()
    agent_service = SimpleNamespace(registry="registry")
    build_runtime(context, agent_service)

    assert captured == {
        "bus": "bus",
        "session_manager": context.sessions,
        "channel_manager": "channels",
        "event_hub": context,
        "tool_registry": "tools",
        "tool_service": context.tools,
        "mcp_service": None,
        "agent_manager": "profiles",
        "agent_registry": "registry",
        "agent_service": agent_service,
        "attachments": None,
        "traces": None,
        "system_prompt": "prompt",
        "hook_runtime": "hooks",
        "session_events": None,
        "llm_service": context.llm,
    }
