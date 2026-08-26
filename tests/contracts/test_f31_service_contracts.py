"""F31 冻结的 Service/Hook 公开契约测试。

测试使用本文件内的极小 Fake 代替磁盘、网络和真实 Agent；Fake 只验证调用形状，
不进入生产模块，也不替生产 Service 增加新的 Port 或 Protocol。
"""

from __future__ import annotations

import asyncio
import inspect

from ftre_agent import AgentService, InboundMessage

from ftre.services.agent.profile.models import EffectiveProfile
from ftre.services.agent.profile.service import AgentProfileService
from ftre.services.config.service import ConfigService
from ftre.services.messaging.bus.service import MessageBusService
from ftre.services.session.service import SessionService
from ftre.services.system_prompt.service import SystemPromptService
from ftre.services.system_prompt.types import PromptAssembly, PromptSection
from ftre.services.tools.service import ToolService


class _FakeAgentRuntime:
    """只覆盖 AgentService 的 runtime 绑定方法集，不模拟 ReAct。"""

    def is_active_session(self, session_id: str) -> bool:
        return session_id == "busy"

    def get_session_status(self, session_id: str) -> str:
        return "running" if session_id == "busy" else "idle"

    async def run_inbound(self, message: InboundMessage) -> InboundMessage:
        return message

    async def cancel_session(self, *args, **kwargs) -> bool:
        return True

    async def delete_session(self, session_id: str) -> str:
        return session_id

    async def resume_confirmation(self, session_id, channel_id, events, metadata):
        return {"session_id": session_id, "channel_id": channel_id, "events": events}


class _FakeProfileManager:
    """Profile Service 的测试替身；实际目录和校验仍由生产 Manager 负责。"""

    def load(self, agent_id: str):
        return {"id": agent_id, "llm": {"model": "fake"}}


def _method_names(cls: type) -> set[str]:
    """只收集类方法名，避免测试绑定实例状态或执行外部副作用。"""
    return {
        name
        for name, value in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_agent_service_public_boundary_accepts_one_inbound_message() -> None:
    """AgentService 接受 InboundMessage，不暴露 QueueItem 或 TurnExecutor。"""
    expected = {
        "run",
        "cancel",
        "status",
        "is_busy",
        "get_session_status",
        "is_session_busy",
        "delete_session",
        "resume_confirmation",
        "list",
        "get",
        "tool_scope",
        "scope_identity",
        "scope_carrier",
    }
    assert expected <= _method_names(AgentService)
    service = AgentService()
    service.attach_runtime(_FakeAgentRuntime())
    message = InboundMessage("s1", "r1", "ws", "hello")
    assert asyncio.run(service.run(message)) == message
    assert service.status("s1") == "idle"
    assert service.is_busy("s1") is False


def test_session_and_message_bus_public_methods_are_stable() -> None:
    """Session/MessageBus 只冻结真实存在的方法；F32 的新出口另有明确输入。"""
    assert {
        "get_session",
        "get_session_metadata",
        "get_context_messages",
        "save_message",
        "update_message",
        "upsert_message",
    } <= _method_names(SessionService)
    assert {"publish_inbound", "request_inbound", "stop_inbound", "start", "close"} <= _method_names(
        MessageBusService
    )
    # Runtime/Session 只通过这一处发布出站事件，不把底层 EventBus 当作契约。
    assert "publish_outbound" in _method_names(MessageBusService)


def test_tool_service_view_and_prompt_assembly_contracts() -> None:
    """Tool View 和同步 PromptAssembly 均通过现有 Service 入口验证。"""
    tools = ToolService()
    assert {"register", "restrict", "snapshot", "schemas", "prepare_view", "execute"} <= _method_names(
        ToolService
    )
    assert tools.schemas("agent-a") == []
    # F34：底层 registry 已私有化；view 是独立实例且不再硬编码内置工具。
    view = asyncio.run(tools.prepare_view("agent-a", session_id="session-a"))
    assert view is not None
    assert len(view) == 0

    prompts = SystemPromptService()
    prompts.register_section(PromptSection(name="base", content="hello", owner="test"))
    assembly = prompts.assemble_result("agent-a", "session-a", workspace="repo")
    assert isinstance(assembly, PromptAssembly)
    assert assembly.text == "hello"
    assert assembly.contributions[0].owner == "test"
    assert not inspect.iscoroutinefunction(SystemPromptService.assemble_result)


def test_profile_service_returns_existing_effective_profile_snapshot() -> None:
    """Profile Service 返回当前 EffectiveProfile，不新增第二份 Profile DTO。"""
    service = AgentProfileService(_FakeProfileManager())
    result = service.resolve("agent-a", session_id="session-a")
    assert isinstance(result, EffectiveProfile)
    assert result.agent_id == "agent-a"
    assert result.value["llm"]["model"] == "fake"
    assert not inspect.iscoroutinefunction(AgentProfileService.resolve)


def test_config_service_resolves_agent_config_from_its_snapshot(tmp_path, monkeypatch) -> None:
    """自定义 ConfigService 的快照必须是 Agent Runtime 的配置事实源。"""
    config = ConfigService(
        tmp_path / "config.json",
        {
            "default_workspace": "C:/snapshot-workspace",
            "agents": {
                "defaults": {
                    "provider": "demo",
                    "model": "demo-model",
                    "reasoning_effort": "high",
                }
            },
            "providers": {
                "demo": {
                    "api_key": "test-key",
                    "api_base": "https://example.invalid/v1",
                    "api_type": "completions",
                    "models": [
                        {
                            "id": "demo-model",
                            "context_window": 1000,
                            "max_output": 100,
                            "reasoning_effort_values": ["high"],
                        }
                    ],
                }
            },
        },
    )
    monkeypatch.setattr(
        "ftre.services.agent.config.load_config",
        lambda: (_ for _ in ()).throw(AssertionError("must not read global loader")),
    )

    resolved = config.resolve_agent_config()

    assert resolved.llm.provider == "demo"
    assert resolved.llm.model == "demo-model"
    assert resolved.llm.context_window == 1000
    assert resolved.llm.reasoning_effort == "high"
    assert resolved.workspace == "C:/snapshot-workspace"
