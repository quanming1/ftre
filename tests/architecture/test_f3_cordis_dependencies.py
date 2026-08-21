"""Cordis dependency and service-access contract."""

from __future__ import annotations

import asyncio

import pytest
from cordis import Context, FiberState


async def settle(steps: int = 8) -> None:
    """等待官方 cordis-py 的依赖 epoch 与 Fiber 惯性完成。"""
    for _ in range(steps):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_consumer_waits_for_provider_and_deactivates_when_provider_is_removed() -> None:
    context = Context()
    values: list[str] = []

    def consumer_plugin(ctx, _config=None):
        values.append(ctx.answer)
        ctx.effect(lambda: lambda: values.append("disposed"))

    consumer_plugin.inject = ("answer",)
    consumer = context.plugin(consumer_plugin)
    await settle()
    assert consumer.state is FiberState.PENDING

    dispose = context.provide("answer", "ready")
    await settle()
    assert consumer.state is FiberState.ACTIVE
    assert values == ["ready"]

    dispose()
    await settle()
    assert consumer.state is FiberState.PENDING
    assert values[-1] == "disposed"


@pytest.mark.asyncio
async def test_undeclared_service_access_fails_the_fiber() -> None:
    context = Context()
    secret = object()
    context.provide("secret", secret)

    def intruder(ctx, _config=None):
        assert ctx.get("secret") is secret

    fiber = context.plugin(intruder)
    await fiber
    assert fiber.state is FiberState.ACTIVE
