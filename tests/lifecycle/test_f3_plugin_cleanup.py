"""Cordis Fiber effect lifecycle contract."""

from __future__ import annotations

import asyncio

import pytest
from cordis import Context, FiberState


async def settle(steps: int = 8) -> None:
    for _ in range(steps):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_effects_run_in_lifo_order_and_dispose_is_idempotent() -> None:
    context = Context()
    cleanup: list[int] = []

    def apply(ctx, _config=None):
        ctx.effect(lambda: lambda: cleanup.append(1))
        ctx.effect(lambda: lambda: cleanup.append(2))

    fiber = context.plugin(apply)
    await fiber
    await fiber.dispose()
    assert cleanup == [2, 1]
    assert fiber.state is FiberState.DISPOSED


@pytest.mark.asyncio
async def test_failed_plugin_rolls_back_effects_before_reporting_failure() -> None:
    context = Context()
    cleanup: list[str] = []

    def apply(ctx, _config=None):
        ctx.effect(lambda: lambda: cleanup.append("rollback"))
        raise RuntimeError("boom")

    fiber = context.plugin(apply)
    await settle()
    assert fiber.state is FiberState.FAILED
    assert cleanup == ["rollback"]
