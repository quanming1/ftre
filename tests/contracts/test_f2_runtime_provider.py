from __future__ import annotations

from ftre_agent import AgentService
from ftre_agent_runtime import plugin


def test_runtime_plugin_binds_service_and_private_loop(monkeypatch) -> None:
    """Runtime Plugin 是 agents Service 的唯一装配点：提供契约 Service、
    构造私有 Loop 并把两者绑定到同一 Fiber（PRD-F33 §5.2）。"""
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

    monkeypatch.setattr(plugin, "AgentLoop", FakeLoop)

    provided = {}
    effects = []

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

        def provide(self, key, value):
            provided[key] = value

        def effect(self, dispose, label=None):
            effects.append((label, dispose))

    context = FakeContext()
    plugin.apply(context)

    # 唯一公开 Service 是 agents；Loop 不出现在公开注册表里。
    assert set(provided) == {"agents"}
    service = provided["agents"]
    assert isinstance(service, AgentService)
    assert captured["agent_service"] is service
    assert service.runtime is not None
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

    # 卸载效应：停止 Loop 并解除绑定，重复 detach 安全。
    assert effects and effects[0][0] == "agent:runtime"

    import asyncio

    dispose = effects[0][1]()
    asyncio.run(dispose())
    assert stopped == [True]
    assert service.list() == []
