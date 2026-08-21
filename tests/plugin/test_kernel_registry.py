"""Cordis dependency and service-access contract."""

from __future__ import annotations

import pytest

from cordis import Context, FiberState, ServiceAccessError


@pytest.mark.asyncio
async def test_consumer_waits_for_provider_and_deactivates_when_provider_is_removed() -> None:
    context = Context()
    values: list[str] = []

    class Consumer:
        inject = ("answer",)

        def apply(self, ctx):
            values.append(ctx.answer)
            ctx.effect(lambda: values.append("disposed"))

    consumer = context.plugin(Consumer, id="consumer")
    await context.settle()
    assert consumer.state is FiberState.PENDING

    dispose = context.provide("answer", "ready")
    await context.settle()
    assert consumer.state is FiberState.ACTIVE
    assert values == ["ready"]

    dispose()
    await context.settle()
    assert consumer.state is FiberState.PENDING
    assert values[-1] == "disposed"


@pytest.mark.asyncio
async def test_undeclared_service_access_fails_the_fiber() -> None:
    context = Context()
    context.provide("secret", object())

    class Intruder:
        def apply(self, ctx):
            ctx.get("secret")

    fiber = context.plugin(Intruder, id="intruder")
    await context.settle()
    assert fiber.state is FiberState.FAILED
    assert isinstance(fiber.error, ServiceAccessError)
