from __future__ import annotations

import asyncio

import pytest
from cordis import Context, FiberState


async def settle(steps: int = 8) -> None:
    for _ in range(steps):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_injected_plugin_waits_for_provider_and_reactivates() -> None:
    ctx = Context()
    events: list[str] = []

    def consumer_plugin(plugin_ctx, _config=None):
        events.append(plugin_ctx.answer)
        plugin_ctx.effect(lambda: lambda: events.append("disposed"))

    consumer_plugin.inject = ("answer",)
    consumer = ctx.plugin(consumer_plugin)
    await settle()
    assert consumer.state is FiberState.PENDING
    provider = ctx.provide("answer", "ready")
    await settle()
    assert consumer.state is FiberState.ACTIVE
    assert events == ["ready"]
    provider()
    await settle()
    assert consumer.state is FiberState.PENDING
    assert events[-1] == "disposed"
    ctx.provide("answer", "again")
    await settle()
    assert consumer.state is FiberState.ACTIVE
    cleanup = ctx.dispose()
    if cleanup is not None:
        await cleanup
    assert events[-1] == "disposed"


@pytest.mark.asyncio
async def test_context_dispose_is_reversible_and_idempotent() -> None:
    ctx = Context()
    cleanups: list[str] = []

    def plugin(plugin_ctx, _config=None):
        plugin_ctx.effect(lambda: lambda: cleanups.append("effect"))

    ctx.plugin(plugin)
    await settle()
    first = ctx.dispose()
    if first is not None:
        await first
    second = ctx.dispose()
    if second is not None:
        await second
    assert cleanups == ["effect"]
