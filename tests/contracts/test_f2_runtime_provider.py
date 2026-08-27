from __future__ import annotations

from ftre_agent import AgentService
from ftre_agent_runtime import plugin


def test_runtime_plugin_registers_private_loop_with_agent_service(monkeypatch) -> None:
    """Runtime Plugin 只构造私有 Loop，并注册到已有的 agents。"""
    captured = {}
    started = []
    stopped = []

    class FakeLoop:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self) -> None:
            started.append(True)

        async def stop(self) -> None:
            stopped.append(True)

        async def run_inbound(self, message):
            return message

        async def cancel_session(self, *args, **kwargs):
            return True

        def get_session_status(self, _session_id):
            return "idle"

        def is_active_session(self, _session_id):
            return False

        async def delete_session(self, _session_id):
            return None

        async def resume_confirmation(self, *args, **kwargs):
            return None

    monkeypatch.setattr(plugin, "AgentLoop", FakeLoop)

    provided = {}
    effects = []

    class FakeContext:
        agents = AgentService()
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

        def provide(self, key, value):
            provided[key] = value

        def effect(self, dispose, label=None):
            effects.append((label, dispose))

    context = FakeContext()
    context.agents.start()
    plugin.apply(context)

    # Runtime 不再提供 Service，只注册到已有的 agents。
    assert provided == {}
    service = context.agents
    assert service.is_ready()
    assert service.factory_name == "ftre-agent-runtime"
    assert started == [True]

    # 注入映射：inject 声明的每个 Service 都按窄公开 key 传入 Loop。
    assert captured == {
        "message_bus": context.message_bus,
        "sessions": context.sessions,
        "tools": context.tools,
        "workspaces": context.workspaces,
        "profiles": context.agent_profiles,
        "config_service": context.config,
        "agent_service": service,
        "attachments": None,
        "traces": None,
        "system_prompt": "prompt",
        "hook_runtime": "hooks",
        "session_events": None,
        "llm_service": context.llm,
    }

    # 卸载效应：停止 Loop 并解除 Factory 注册。
    assert effects and effects[0][0] == "agent:runtime"

    import asyncio

    dispose = effects[0][1]()
    asyncio.run(dispose())
    assert stopped == [True]
    assert not service.is_ready()
    assert service.list() == []
