"""Cordis Fiber effect lifecycle contract."""

from __future__ import annotations

import pytest

from cordis import Context, FiberState


@pytest.mark.asyncio
async def test_effects_run_in_lifo_order_and_dispose_is_idempotent() -> None:
    context = Context()
    cleanup: list[int] = []

    def apply(ctx):
        ctx.effect(lambda: cleanup.append(1))
        ctx.effect(lambda: cleanup.append(2))

    fiber = context.plugin(apply, id="effects")
    await context.settle()
    await context.unload("effects")
    await fiber.dispose()
    assert cleanup == [2, 1]
    assert fiber.state is FiberState.DISPOSED


@pytest.mark.asyncio
async def test_failed_plugin_rolls_back_effects_before_reporting_failure() -> None:
    context = Context()
    cleanup: list[str] = []

    def apply(ctx):
        ctx.effect(lambda: cleanup.append("rollback"))
        raise RuntimeError("boom")

    fiber = context.plugin(apply, id="broken")
    await context.settle()
    assert fiber.state is FiberState.FAILED
    assert cleanup == ["rollback"]
