"""F33 Agent Package 契约测试。

冻结两个 Package 的公开契约：AgentService 的 runtime 绑定语义、
AgentRunResult 稳定状态、CompletionRegistry 的进程内等待，以及 Runtime
Plugin 在真实 Composition 中的生命周期（装载、卸载、重复关闭）。
"""

from __future__ import annotations

import asyncio

import pytest
from ftre_agent import (
    AgentConfig,
    AgentRegistry,
    AgentRunResult,
    AgentService,
    InboundMessage,
)
from ftre_agent_runtime.completion import CompletionRegistry


class _RecordingRuntime:
    """实现 AgentService 约定方法集的最小 runtime。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_active_session(self, session_id: str) -> bool:
        return False

    def get_session_status(self, session_id: str) -> str:
        return "idle"

    async def run_inbound(self, message: InboundMessage) -> AgentRunResult:
        self.calls.append("run")
        return AgentRunResult(
            session_id=message.session_id, turn_id="t", status="completed"
        )

    async def cancel_session(self, *args, **kwargs) -> bool:
        self.calls.append("cancel")
        return True

    async def delete_session(self, session_id: str) -> None:
        self.calls.append("delete")

    async def resume_confirmation(self, *args, **kwargs):
        self.calls.append("resume")


def test_runtime_binding_is_idempotent_and_rejects_second_runtime() -> None:
    service = AgentService()
    runtime = _RecordingRuntime()
    service.attach_runtime(runtime)
    service.attach_runtime(runtime)  # 同一实例幂等
    with pytest.raises(RuntimeError, match="already has an attached runtime"):
        service.attach_runtime(_RecordingRuntime())
    assert service.runtime is runtime


@pytest.mark.asyncio
async def test_service_run_returns_stable_agent_run_result() -> None:
    service = AgentService()
    service.attach_runtime(_RecordingRuntime())
    result = await service.run(InboundMessage("s1", "r1", "ws", "hello"))
    assert isinstance(result, AgentRunResult)
    assert result.status == "completed"
    assert result.session_id == "s1"
    assert result.error is None


def test_agent_run_result_rejects_nothing_but_freezes_shape() -> None:
    """契约字段在包化后保持 PRD 形状；错误上下文只在失败路径出现。"""
    failed = AgentRunResult(
        session_id="s",
        turn_id="t",
        status="failed",
        error={"code": "agent-busy", "retryable": True},
    )
    assert failed.status == "failed"
    assert failed.error["code"] == "agent-busy"
    cancelled = AgentRunResult(session_id="s", turn_id="t", status="cancelled")
    assert cancelled.final_content == ""


@pytest.mark.asyncio
async def test_completion_registry_waits_and_caches_once() -> None:
    registry = CompletionRegistry()
    outcome = AgentRunResult(session_id="s", turn_id="t", status="completed")

    async def producer() -> None:
        await asyncio.sleep(0)
        await registry.complete("s", "r", outcome)

    consumer = asyncio.create_task(registry.wait("s", "r"))
    await producer()
    assert await consumer is outcome
    # 已完成结果进入有限缓存：后到的 wait 立即返回同一对象。
    assert await registry.wait("s", "r") is outcome


@pytest.mark.asyncio
async def test_completion_registry_close_rejects_new_waiters() -> None:
    registry = CompletionRegistry()
    waiter = asyncio.create_task(registry.wait("s", "late"))
    await asyncio.sleep(0)
    await registry.close()
    with pytest.raises(RuntimeError, match="AgentLoop 已关闭"):
        await waiter


def test_registry_scope_carrier_uses_agent_key_and_parent_chain() -> None:
    registry = AgentRegistry()
    registry.ensure("leader")
    registry.ensure("member")
    carrier = registry.scope_carrier("member", parent_id="leader")
    assert carrier.key == "agent"
    identities = carrier.identities
    assert registry.scope_identity("member") in identities
    assert registry.scope_identity("leader") in identities
    # kernel 的机制函数按鸭子类型消费 carrier。
    from cordis import Context

    from ftre.kernel.hooks import context_for_scope

    scoped = context_for_scope(Context(), carrier)
    assert scoped is not None


def test_agent_config_contract_fields_are_stable() -> None:
    config = AgentConfig()
    assert config.llm.model == ""
    assert config.title_llm is None
    assert config.workspace == ""
