"""F34 生命周期测试：core-tools / tool-audit Plugin 的可逆性与审计行为。

core-tools 卸载后内置工具贡献消失、后续 view 不含内置工具；
tool-audit 每次 tool/after 输出一行结构化日志，卸载后监听随之消失。
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from cordis import Context, FiberState
from ftre_agent import AgentRegistry
from ftre_agent_core.hooks import (
    ToolAfterPayload,
    ToolCallIdentity,
    ToolExecutionResult,
)
from ftre_agent_core.tool import ToolRegistry

from ftre.kernel.hooks import HookRuntime
from ftre.plugins.builtin.core_tools.plugin import apply as core_tools_apply
from ftre.plugins.builtin.tool_audit.plugin import apply as tool_audit_apply
from ftre.services.tools import ToolService
from ftre.services.tools.hooks import TOOL_AFTER_SPEC

CORE_TOOL_NAMES = {"bash", "read", "write", "edit", "set_workspace"}


def _core_tools_plugin(ctx, _config=None):
    return core_tools_apply(ctx, _config)


_core_tools_plugin.inject = ("tools",)
_core_tools_plugin.provide = ()


def _tool_audit_plugin(ctx, _config=None):
    return tool_audit_apply(ctx, _config)


_tool_audit_plugin.inject = ("hook_runtime",)
_tool_audit_plugin.provide = ()


def _has_tool_after_listener(runtime: HookRuntime) -> bool:
    """诊断快照里是否仍有 tool-audit 的 tool/after 活跃监听。"""
    return any(
        item.hook == TOOL_AFTER_SPEC.name
        and item.owner == "tool-audit"
        and not item.disposed
        for item in runtime.snapshot()
    )


def _agent_dispatch_context(runtime: HookRuntime) -> Context:
    """tool/after 是 Agent-scoped Hook，dispatch 必须携带 isolate Context。"""
    registry = AgentRegistry()
    registry.ensure("default")
    return runtime.context_for_scope(registry.scope_carrier("default"))


@pytest.mark.asyncio
async def test_core_tools_unload_removes_builtin_tools_from_future_views() -> None:
    """卸载 core-tools 后贡献与 view 中的内置工具一起消失（可逆、幂等）。"""
    root = Context()
    tools = ToolService(ToolRegistry())
    root.provide("tools", tools)
    fiber = root.plugin(_core_tools_plugin)
    await fiber
    assert fiber.state is FiberState.ACTIVE

    view_before = await tools.prepare_view("default", "session-1")
    assert CORE_TOOL_NAMES <= set(view_before.names)

    cleanup = fiber.dispose()
    if cleanup is not None:
        await cleanup

    assert CORE_TOOL_NAMES.isdisjoint({item.name for item in tools.snapshot()})
    view_after = await tools.prepare_view("default", "session-1")
    assert CORE_TOOL_NAMES.isdisjoint(set(view_after.names))

    cleanup = root.dispose()
    if cleanup is not None:
        await cleanup


@pytest.mark.asyncio
async def test_core_tools_failure_rolls_back_contributions() -> None:
    """Plugin 失败时已注册的贡献随 Fiber 回滚，不泄漏内置工具。"""
    root = Context()
    tools = ToolService(ToolRegistry())
    root.provide("tools", tools)

    def failing_plugin(ctx, _config=None):
        core_tools_apply(ctx, _config)
        raise RuntimeError("boom")

    failing_plugin.inject = ("tools",)
    fiber = root.plugin(failing_plugin)
    with pytest.raises(RuntimeError, match="boom"):
        await fiber.await_()
    assert fiber.state is FiberState.FAILED
    assert tools.snapshot() == ()
    cleanup = root.dispose()
    if cleanup is not None:
        await cleanup


@pytest.mark.asyncio
async def test_tool_audit_logs_one_structured_line_per_tool_call(caplog) -> None:
    """tool/after 每次调用输出一行结构化审计日志（AC7）。"""
    root = Context()
    runtime = HookRuntime(root)
    root.provide("hook_runtime", runtime)
    fiber = root.plugin(_tool_audit_plugin)
    await fiber
    assert fiber.state is FiberState.ACTIVE

    payload = ToolAfterPayload(
        ToolCallIdentity("call-1", "bash", "session-1", "turn-1", "default", 2),
        {"command": "ls"},
        ToolExecutionResult(output="done"),
        asyncio.Event(),
    )
    with caplog.at_level(logging.INFO, logger="ftre.tool_audit"):
        result = await runtime.dispatch(
            TOOL_AFTER_SPEC, payload, context=_agent_dispatch_context(runtime)
        )

    # 观察型消费者不改变结果协议。
    assert isinstance(result, ToolExecutionResult)
    assert result.output == "done"

    records = [r for r in caplog.records if r.name == "ftre.tool_audit"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "session_id=session-1" in message
    assert "agent_id=default" in message
    assert "turn_id=turn-1" in message
    assert "call_id=call-1" in message
    assert "name=bash" in message
    assert "status=completed" in message

    cleanup = root.dispose()
    if cleanup is not None:
        await cleanup


@pytest.mark.asyncio
async def test_tool_audit_unload_removes_listener_and_silences_log(caplog) -> None:
    """卸载 tool-audit 后 Hook 监听消失，不再输出审计日志（AC6）。"""
    root = Context()
    runtime = HookRuntime(root)
    root.provide("hook_runtime", runtime)
    fiber = root.plugin(_tool_audit_plugin)
    await fiber
    assert _has_tool_after_listener(runtime)

    cleanup = fiber.dispose()
    if cleanup is not None:
        await cleanup
    assert not _has_tool_after_listener(runtime)

    payload = ToolAfterPayload(
        ToolCallIdentity("call-2", "read", "session-1", "turn-1", "default"),
        {"path": "a.txt"},
        ToolExecutionResult(output=""),
        asyncio.Event(),
    )
    with caplog.at_level(logging.INFO, logger="ftre.tool_audit"):
        await runtime.dispatch(
            TOOL_AFTER_SPEC, payload, context=_agent_dispatch_context(runtime)
        )
    assert not [r for r in caplog.records if r.name == "ftre.tool_audit"]

    cleanup = root.dispose()
    if cleanup is not None:
        await cleanup
