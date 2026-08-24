"""F15 生命周期与顺序回归。

这些测试刻意使用 Event/Barrier，不用 sleep 猜测调度：业务 Hook 的卸载必须先阻止
新调用，再等待已经进入的 listener 完成；权威 Session 生命周期通知也必须等待异步
观察者结束。
"""

from __future__ import annotations

import asyncio

import pytest
from cordis import Context

from ftre.kernel.hooks import HookMode, HookRuntime, HookSpec
from ftre.services.session.hooks import SESSION_DISPOSED_SPEC, SessionLifecyclePayload


@pytest.mark.asyncio
async def test_unload_waits_for_inflight_parallel_listener():
    context = Context()
    runtime = HookRuntime(context)
    started = asyncio.Event()
    release = asyncio.Event()
    spec = HookSpec(
        "session/disposed",
        "session",
        HookMode.PARALLEL,
        payload_type=SessionLifecyclePayload,
        result_type=type(None),
        default=lambda _payload: None,
    )

    async def blocking(_payload):
        started.set()
        await release.wait()

    runtime.register(spec, blocking, owner="f15-test", context=context)
    dispatch_task = asyncio.create_task(
        runtime.dispatch(spec, SessionLifecyclePayload("s1"))
    )
    await started.wait()
    cleanup = context.dispose()
    cleanup_task = cleanup
    await asyncio.sleep(0)
    assert runtime.snapshot(spec.name)[0].disposed is True
    assert cleanup_task is not None and not cleanup_task.done()
    release.set()
    await dispatch_task
    await cleanup_task


@pytest.mark.asyncio
async def test_session_disposed_listener_is_awaited_and_failure_is_observed():
    context = Context()
    runtime = HookRuntime(context)
    seen: list[str] = []

    async def observer(payload):
        await asyncio.sleep(0)
        seen.append(payload.session_id)
        raise RuntimeError("observer detail must not escape")

    runtime.register(
        SESSION_DISPOSED_SPEC,
        observer,
        owner="f15-observer",
        context=context,
        all_agent_scopes=True,
    )
    await runtime.dispatch(
        SESSION_DISPOSED_SPEC,
        SessionLifecyclePayload("s2", reason="delete"),
    )
    assert seen == ["s2"]
    assert runtime.diagnostics[-1].hook == "session/disposed"
    cleanup = context.dispose()
    if cleanup is not None:
        await cleanup
