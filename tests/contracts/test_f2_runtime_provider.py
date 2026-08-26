from __future__ import annotations

from ftre.services.agent.runtime import provider
from ftre.services.agent.runtime.provider import build_runtime


def test_runtime_provider_maps_public_services_to_loop(monkeypatch) -> None:
    captured = {}

    class FakeLoop:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(provider, "AgentLoop", FakeLoop)
    class FakeContext:
        config = object()
        sessions = object()
        message_bus = object()
        tools = object()
        workspaces = object()
        agent_profiles = object()
        system_prompt = "prompt"
        hook_runtime = "hooks"
        session_events = None
        llm = object()

        def get(self, key, strict=False):
            return {"attachments": None, "traces": None}.get(key)

    context = FakeContext()
    agent_service = object()
    build_runtime(context, agent_service)

    assert captured == {
        "message_bus": context.message_bus,
        "sessions": context.sessions,
        "tools": context.tools,
        "workspaces": context.workspaces,
        "profiles": context.agent_profiles,
        "config_service": context.config,
        "agent_service": agent_service,
        "attachments": None,
        "traces": None,
        "system_prompt": "prompt",
        "hook_runtime": "hooks",
        "session_events": None,
        "llm_service": context.llm,
    }
