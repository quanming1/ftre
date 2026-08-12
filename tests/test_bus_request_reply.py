"""用户输入通过 Bus 请求/应答拿到 durable ACK，不能只确认内存入队。"""
from __future__ import annotations

import asyncio

import pytest

from ftre.bus import BusMessage, EventBus


@pytest.mark.asyncio
async def test_request_inbound_resolves_only_when_consumer_replies():
    bus = EventBus()
    request = BusMessage(type="user_message", data={"content": "hello"})
    pending = asyncio.create_task(bus.request_inbound(request))

    inbound = await anext(bus.subscribe_inbound())
    assert not pending.done()
    assert bus.resolve_inbound(inbound.id, {"accepted": True})
    assert await pending == {"accepted": True}


@pytest.mark.asyncio
async def test_stop_inbound_wakes_request_waiter():
    bus = EventBus()
    pending = asyncio.create_task(bus.request_inbound(BusMessage(type="user_message")))
    await anext(bus.subscribe_inbound())
    bus.stop_inbound()
    with pytest.raises(RuntimeError, match="停止"):
        await pending
