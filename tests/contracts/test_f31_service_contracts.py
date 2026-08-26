"""F31 冻结的 Service/Hook 公开契约测试。

测试使用本文件内的极小 Fake 代替磁盘、网络和真实 Agent；Fake 只验证调用形状，
不进入生产模块，也不替生产 Service 增加新的 Port 或 Protocol。
"""

from __future__ import annotations

import asyncio
import inspect

from ftre.services.agent import AgentService, InboundMessage
from ftre.services.agent.profile.models import EffectiveProfile
from ftre.services.agent.profile.service import AgentProfileService
from ftre.services.messaging.bus.service import MessageBusService
from ftre.services.session.service import SessionService
from ftre.services.system_prompt.service import SystemPromptService
from ftre.services.system_prompt.types import PromptAssembly, PromptSection
from ftre.services.tools.service import ToolService


class _FakeAgentDriver:
    """只覆盖 AgentService 现有 Driver 组合契约，不模拟 ReAct。"""

    def is_session_busy(self, session_id: str) -> bool:
        return session_id == "busy"

    def get_session_status(self, session_id: str) -> str:
        return "running" if session_id == "busy" else "idle"

    def is_busy(self, session_id: str) -> bool:
        return self.is_session_busy(session_id)

    async def run(self, message: InboundMessage) -> InboundMessage:
        return message

    async def cancel(self, *args, **kwargs) -> bool:
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
    service.attach_driver(_FakeAgentDriver())
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
    # 出站窄方法是 F32 输入，不在 F31 伪造实现，也不把底层 EventBus 当作契约。
    assert "publish_outbound" not in _method_names(MessageBusService)


def test_tool_service_view_and_prompt_assembly_contracts() -> None:
    """Tool View 和同步 PromptAssembly 均通过现有 Service 入口验证。"""
    tools = ToolService()
    assert {"register", "restrict", "snapshot", "schemas", "build_view", "execute"} <= _method_names(
        ToolService
    )
    assert tools.schemas("agent-a") == []
    view = tools.build_view("agent-a", session_id="session-a")
    assert view is not tools.registry

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
