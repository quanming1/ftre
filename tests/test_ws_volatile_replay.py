"""WebSocket 只通过 Bus request/reply 取得 durable admission ACK。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from ftre.bus import EventBus
from ftre.channel.ws_channel import WebSocketChannel


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


@pytest.mark.asyncio
async def test_attach_reads_agent_loop_mailbox_snapshot():
    channel = WebSocketChannel(EventBus())
    channel._session_projection = AsyncMock()
    channel._session_projection.snapshot = AsyncMock(return_value=[])
    channel._session_projection.session_event_snapshot = AsyncMock(return_value=[])
    channel._session_snapshot_provider = AsyncMock()
    channel._session_snapshot_provider.get_mailbox_snapshot = AsyncMock(
        return_value={"revision": 3, "pending": [{"request_id": "request-b"}]}
    )

    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "data": {"session_id": "ws_sess_test"}}), ws
    )

    assert ws.sent[0]["type"] == "reply_snapshot"
    assert ws.sent[0]["data"]["mailbox"]["pending"][0]["request_id"] == "request-b"


@pytest.mark.asyncio
async def test_user_message_ack_waits_for_bus_reply():
    bus = EventBus()
    channel = WebSocketChannel(bus)
    ws = FakeWebSocket()
    received = asyncio.create_task(channel._on_message(
        json.dumps({
            "frame_id": "client-frame-1",
            "type": "user_message",
            "data": {"session_id": "ws_sess_test", "content": "hello"},
        }),
        ws,
    ))

    inbound = await anext(bus.subscribe_inbound())
    assert not received.done()
    bus.resolve_inbound(
        inbound.id,
        SimpleNamespace(
            accepted=True,
            session_id="ws_sess_test",
            request_id="request-a",
            queue_position=1,
            created=True,
        ),
    )
    await received

    assert ws.sent == [{
        "frame_id": "client-frame-1",
        "type": "message_ack",
        "data": {
            "session_id": "ws_sess_test",
            "request_id": "request-a",
                "queue_position": 1,
            "created": True,
        },
        "metadata": {"channel_id": "ws", "session_id": "ws_sess_test"},
    }]


@pytest.mark.asyncio
async def test_user_message_without_frame_id_is_rejected_before_bus():
    bus = EventBus()
    channel = WebSocketChannel(bus)
    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "user_message", "data": {"session_id": "s", "content": "x"}}), ws
    )
    assert ws.sent[0]["data"]["code"] == "missing_request_id"
    assert bus._inbound_queue.empty()
