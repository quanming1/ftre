from __future__ import annotations

import asyncio

import pytest
from cordis import Context
from ftre_agent import AgentRegistry
from ftre_agent.hooks import (
    AGENT_BEFORE_REASONING_SPEC,
    BeforeReasoningPayload,
)
from ftre_inbox.plugin import apply
from ftre_inbox.protocol import InboundMessage

from ftre.kernel.hooks import HookRuntime


class BusyAgent:
    def is_busy(self, _session_id: str) -> bool:
        return True


class SessionRoot:
    def __init__(self, root):
        self.root = root

    def sessions_root(self):
        return self.root

    def has_session(self, _session_id: str) -> bool:
        return True


def _provide_plugin_dependencies(context: Context) -> None:
    """为 Inbox Plugin 提供它真正声明的公开 Service 依赖。

    这些测试只验证队列 admission 与 Runtime Hook 的连接，不需要真实的
    SessionEventService；但 `session_events` 仍是 Inbox 的必需注入边界，
    因此用显式的空能力填充最小测试上下文，而不是让 Plugin 回退到隐式
    `ctx.get()`。
    """
    context.provide("session_events", None)


@pytest.mark.asyncio
async def test_plugin_consumes_next_step_through_core_hook(tmp_path):
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("sessions", None)
    context.provide("agents", BusyAgent())
    _provide_plugin_dependencies(context)

    await apply(context, {"inbox_dir": str(tmp_path)})
    inbox = context.get("inbox", strict=False)
    await inbox.steer(InboundMessage("s1", "steer-1", "ws", "请改用中文"))

    registry = AgentRegistry()
    registry.ensure("default")
    scope = runtime.context_for_scope(registry.scope_carrier("default"))
    result = await runtime.dispatch(
        AGENT_BEFORE_REASONING_SPEC,
        BeforeReasoningPayload(
            agent=object(),
            session_id="s1",
            turn_id="turn-1",
            iteration=2,
            cancellation=asyncio.Event(),
        ),
        context=scope,
    )

    assert [message["content"] for message in result.messages] == ["请改用中文"]
    assert not (await inbox.snapshot("s1")).has_pending
    cleanup = context.dispose()
    if cleanup is not None:
        await cleanup


@pytest.mark.asyncio
async def test_plugin_uses_session_root_when_inbox_dir_is_not_configured(tmp_path):
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("sessions", SessionRoot(tmp_path / "sessions"))
    context.provide("agents", BusyAgent())
    _provide_plugin_dependencies(context)

    await apply(context)
    inbox = context.get("inbox", strict=False)
    assert inbox.repository.root == tmp_path / "sessions" / "_inbox"
    cleanup = context.dispose()
    if cleanup is not None:
        await cleanup


@pytest.mark.asyncio
async def test_steer_hook_consumed_before_second_llm_call(tmp_path):
    """steer 在两次 LLM 调用之间被 before-reasoning Hook 原子消费。

    用最小 stub 驱动两轮 dispatch（等价于 Runtime 在每个 Reasoning 前
    消费 next-step 队列），避免依赖 Runtime 之外的执行实现。
    """
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("sessions", None)
    context.provide("agents", BusyAgent())
    _provide_plugin_dependencies(context)
    await apply(context, {"inbox_dir": str(tmp_path)})
    inbox = context.get("inbox", strict=False)
    registry = AgentRegistry()
    registry.ensure("default")
    scope = runtime.context_for_scope(registry.scope_carrier("default"))

    async def reason_once(iteration: int):
        return await runtime.dispatch(
            AGENT_BEFORE_REASONING_SPEC,
            BeforeReasoningPayload(
                agent=object(),
                session_id="s1",
                turn_id="turn-1",
                iteration=iteration,
                cancellation=asyncio.Event(),
            ),
            context=scope,
        )

    # 第一轮 Reasoning：队列空，无注入。
    first = await reason_once(1)
    assert list(first.messages) == []

    # 用户 steer 进队；下一轮 Reasoning 必须原子取出并清空 pending。
    await inbox.steer(InboundMessage("s1", "steer-1", "ws", "请改用中文"))
    second = await reason_once(2)

    assert [message["content"] for message in second.messages] == ["请改用中文"]
    assert not (await inbox.snapshot("s1")).has_pending
    # 第三轮：已消费，不重复注入。
    third = await reason_once(3)
    assert list(third.messages) == []
    cleanup = context.dispose()
    if cleanup is not None:
        await cleanup
