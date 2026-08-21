"""Cordis Context event contract used by runtime hooks."""

from __future__ import annotations

import pytest
from cordis import Context


def test_event_registration_preserves_order_and_is_reversible() -> None:
    context = Context()
    calls: list[str] = []
    dispose_first = context.on("event", lambda: calls.append("first"))
    context.on("event", lambda: calls.append("second"))

    context.emit("event")
    assert calls == ["first", "second"]
    dispose_first()
    dispose_first()
    context.emit("event")
    assert calls == ["first", "second", "second"]


@pytest.mark.asyncio
async def test_waterfall_composes_hook_context_and_default_result() -> None:
    context = Context()

    async def add_one(value: int, next_):
        return (await next_()) + 1

    async def keep(value: int, next_):
        return await next_()

    context.on("hook", add_one)
    context.on("hook", keep)

    async def identity(value: int) -> int:
        return value

    assert await context.waterfall("hook", 1, inner=identity) == 2


@pytest.mark.asyncio
async def test_waterfall_disposer_stops_a_hook_from_running() -> None:
    context = Context()
    calls: list[str] = []
    dispose = context.on("hook", lambda value, next_: calls.append("called") or value)
    dispose()

    async def identity(value):
        return value

    assert await context.waterfall("hook", "value", inner=identity) == "value"
    assert calls == []
