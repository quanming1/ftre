from __future__ import annotations

import pytest

from cordis import Context, FiberState


@pytest.mark.asyncio
async def test_injected_plugin_waits_for_provider_and_reactivates() -> None:
    ctx = Context()
    events: list[str] = []

    class Consumer:
        inject = ("answer",)
        provide = ()

        def apply(self, plugin_ctx):
            events.append(plugin_ctx.answer)
            plugin_ctx.effect(lambda: events.append("disposed"))

    consumer = ctx.plugin(Consumer, id="consumer")
    await ctx.settle()
    assert consumer.state is FiberState.PENDING
    provider = ctx.provide("answer", "ready")
    await ctx.settle()
    assert consumer.state is FiberState.ACTIVE
    assert events == ["ready"]
    provider()
    await ctx.settle()
    assert consumer.state is FiberState.PENDING
    assert events[-1] == "disposed"
    ctx.provide("answer", "again")
    await ctx.settle()
    assert consumer.state is FiberState.ACTIVE
    await ctx.dispose()
    assert events[-1] == "disposed"


@pytest.mark.asyncio
async def test_context_dispose_is_reversible_and_idempotent() -> None:
    ctx = Context()
    cleanups: list[str] = []

    def plugin(plugin_ctx):
        plugin_ctx.effect(lambda: cleanups.append("effect"))

    ctx.plugin(plugin, id="example")
    await ctx.settle()
    await ctx.dispose()
    await ctx.dispose()
    assert cleanups == ["effect"]

