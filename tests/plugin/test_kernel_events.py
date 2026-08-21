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
    assert dispose_first() is True
    assert dispose_first() is False
    context.emit("event")
    assert calls == ["first", "second", "second"]


@pytest.mark.asyncio
async def test_filter_threads_hook_context_and_keeps_none_result() -> None:
    context = Context()

    async def add_one(value: int) -> int:
        return value + 1

    def keep(value: int) -> None:
        return None

    context.on("filter", add_one)
    context.on("filter", keep)
    assert await context.filter("filter", 1) == 2


@pytest.mark.asyncio
async def test_filter_disposer_stops_a_hook_from_running() -> None:
    context = Context()
    calls: list[str] = []
    dispose = context.on("filter", lambda value: calls.append("called") or value)
    dispose()

    assert await context.filter("filter", "value") == "value"
    assert calls == []
