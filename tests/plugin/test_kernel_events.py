"""kernel.events 单元测试：EventHub 的注册与 6 种分发模式。"""

from __future__ import annotations

import asyncio

import pytest

from ftre.plugin.kernel.events import EventHub, is_bailed

# ── is_bailed ─────────────────────────────────────────────────────


def test_is_bailed_treats_none_and_false_as_non_terminal():
    assert is_bailed(None) is False
    assert is_bailed(False) is False
    assert is_bailed(0) is True  # 0 是终止值（非 None/False）
    assert is_bailed("") is True
    assert is_bailed("x") is True


# ── on / emit ─────────────────────────────────────────────────────


def test_emit_calls_listeners_in_registration_order():
    hub = EventHub()
    calls: list[str] = []
    hub.on("evt", lambda: calls.append("a"))
    hub.on("evt", lambda: calls.append("b"))
    hub.emit("evt")
    assert calls == ["a", "b"]


def test_emit_passes_args_to_listeners():
    hub = EventHub()
    received: list[tuple] = []
    hub.on("evt", lambda x, y: received.append((x, y)))
    hub.emit("evt", 1, 2)
    assert received == [(1, 2)]


def test_prepend_inserts_listener_before_existing():
    hub = EventHub()
    calls: list[str] = []
    hub.on("evt", lambda: calls.append("first"))
    hub.on("evt", lambda: calls.append("prepended"), prepend=True)
    hub.emit("evt")
    assert calls == ["prepended", "first"]


def test_emit_ignores_listener_return_values():
    hub = EventHub()
    hub.on("evt", lambda: "ignored")
    assert hub.emit("evt") is None


def test_listener_exception_does_not_break_other_listeners():
    hub = EventHub()
    calls: list[str] = []

    def bad():
        calls.append("bad")
        raise RuntimeError("boom")

    hub.on("evt", bad)
    hub.on("evt", lambda: calls.append("ok"))
    hub.emit("evt")  # 不应抛出
    assert calls == ["bad", "ok"]


# ── once ──────────────────────────────────────────────────────────


def test_once_fires_only_one_time():
    hub = EventHub()
    count = {"n": 0}
    hub.once("evt", lambda: count.__setitem__("n", count["n"] + 1))
    hub.emit("evt")
    hub.emit("evt")
    hub.emit("evt")
    assert count["n"] == 1


# ── disposer ──────────────────────────────────────────────────────


def test_disposer_removes_listener_and_reports_registration_state():
    hub = EventHub()
    calls: list[str] = []
    dispose = hub.on("evt", lambda: calls.append("x"))
    hub.emit("evt")
    assert dispose() is True  # 移除时仍在注册
    hub.emit("evt")
    assert calls == ["x"]
    assert dispose() is False  # 重复移除返回 False


# ── parallel ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_awaits_all_listeners_concurrently():
    hub = EventHub()
    done: list[str] = []

    async def slow(tag: str):
        await asyncio.sleep(0.01)
        done.append(tag)

    hub.on("evt", lambda: slow("a"))
    hub.on("evt", lambda: slow("b"))
    await hub.parallel("evt")
    assert sorted(done) == ["a", "b"]


@pytest.mark.asyncio
async def test_parallel_swallows_listener_exceptions():
    hub = EventHub()
    done: list[str] = []

    async def bad():
        raise RuntimeError("boom")

    hub.on("evt", lambda: bad())
    hub.on("evt", lambda: done.append("ok"))
    await hub.parallel("evt")  # 不应抛出
    assert done == ["ok"]


# ── serial ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_serial_returns_first_bail_value_and_stops():
    hub = EventHub()
    calls: list[str] = []

    async def first():
        calls.append("first")

    async def second():
        calls.append("second")
        return "STOP"  # 终止值，短路

    async def third():
        calls.append("third")
        return "never"

    hub.on("evt", lambda: first())
    hub.on("evt", lambda: second())
    hub.on("evt", lambda: third())
    result = await hub.serial("evt")
    assert result == "STOP"
    assert calls == ["first", "second"]  # third 未被调用


@pytest.mark.asyncio
async def test_serial_returns_none_when_no_listener_bails():
    hub = EventHub()
    hub.on("evt", lambda: None)
    assert await hub.serial("evt") is None


# ── bail（同步短路）─────────────────────────────────────────────


def test_bail_stops_on_first_sync_terminal_value():
    hub = EventHub()
    calls: list[str] = []
    hub.on("evt", lambda: calls.append("a") or None)
    hub.on("evt", lambda: calls.append("b") or "STOP")
    hub.on("evt", lambda: calls.append("c") or "never")
    result = hub.bail("evt")
    assert result == "STOP"
    assert calls == ["a", "b"]


def test_bail_skips_async_listeners():
    hub = EventHub()

    async def async_listener():
        return "async"

    hub.on("evt", lambda: async_listener())
    hub.on("evt", lambda: "sync-stop")
    assert hub.bail("evt") == "sync-stop"


# ── waterfall（中间件链）───────────────────────────────────────


@pytest.mark.asyncio
async def test_waterfall_chains_listeners_around_inner():
    hub = EventHub()
    trace: list[str] = []

    async def outer(value, next):
        trace.append("outer-before")
        result = await next()
        trace.append("outer-after")
        return f"outer({result})"

    async def innermost(value, next):
        trace.append("inner")
        return await next()

    hub.on("evt", lambda v, n: outer(v, n))
    hub.on("evt", lambda v, n: innermost(v, n))

    async def inner(value):
        trace.append("core")
        return "core-result"

    result = await hub.waterfall("evt", "arg", inner=inner)
    assert result == "outer(core-result)"
    assert trace == ["outer-before", "inner", "core", "outer-after"]


@pytest.mark.asyncio
async def test_waterfall_veto_skips_downstream():
    hub = EventHub()
    calls: list[str] = []

    async def veto(value, next):
        calls.append("veto")
        return "blocked"  # 不调 next()，否决下游

    async def downstream(value, next):
        calls.append("downstream")
        return await next()

    hub.on("evt", lambda v, n: veto(v, n))
    hub.on("evt", lambda v, n: downstream(v, n))
    result = await hub.waterfall("evt", "arg")
    assert result == "blocked"
    assert calls == ["veto"]  # downstream 未执行


@pytest.mark.asyncio
async def test_waterfall_with_no_listeners_calls_inner():
    hub = EventHub()

    async def inner(value):
        return f"default:{value}"

    assert await hub.waterfall("evt", "x", inner=inner) == "default:x"
    assert await hub.waterfall("evt", "x") is None  # 无 inner 无监听器


# ── filter（reduce 风格，兼容现有 hook chain）───────────────


@pytest.mark.asyncio
async def test_filter_threads_value_through_listeners():
    hub = EventHub()

    async def add_one(value):
        return value + 1

    async def double(value):
        return value * 2

    hub.on("evt", add_one)
    hub.on("evt", double)
    assert await hub.filter("evt", 10) == 22  # (10+1)*2


@pytest.mark.asyncio
async def test_filter_none_return_keeps_current_value():
    hub = EventHub()

    async def keep(value):
        return None  # 视为未改写

    async def add_one(value):
        return value + 1

    hub.on("evt", keep)
    hub.on("evt", add_one)
    assert await hub.filter("evt", 5) == 6


@pytest.mark.asyncio
async def test_filter_listener_exception_is_skipped():
    hub = EventHub()

    async def bad(value):
        raise RuntimeError("boom")

    async def add_one(value):
        return value + 1

    hub.on("evt", bad)
    hub.on("evt", add_one)
    assert await hub.filter("evt", 1) == 2  # bad 被跳过，add_one 生效


@pytest.mark.asyncio
async def test_filter_supports_sync_listeners():
    hub = EventHub()
    hub.on("evt", lambda v: v.upper())
    assert await hub.filter("evt", "abc") == "ABC"
