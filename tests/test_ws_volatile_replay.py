"""WebSocket attach 与现代 Queue/Status baseline。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ftre.plugins.builtin.channels.websocket.channel import WebSocketChannel
from ftre.services.messaging.bus import EventBus


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.application_state = 1

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _Inbox:
    def __init__(self):
        self.item = SimpleNamespace(request_id="queued-1")

    async def wire_snapshot(self, session_id):
        return {
            "session_id": session_id,
            "items": [{"id": "request-b", "placement": "queued", "message": {"content": []}}],
        }

    async def snapshot(self, _session_id):
        return SimpleNamespace(next_turn=(self.item,), next_step=())

    async def promote(self, _session_id, _item_id):
        return True

    async def edit(self, _session_id, _item_id, _content, _attachments):
        return True

    async def remove(self, _session_id, _item_id):
        return True


@pytest.mark.asyncio
async def test_attach_reads_inbox_queue_and_status_baseline():
    channel = WebSocketChannel(EventBus())
    channel._session_projection = SimpleNamespace(
        snapshot=AsyncMock(return_value=[]),
        session_event_snapshot=AsyncMock(return_value=[]),
    )
    channel.set_inbox_provider(_Inbox())
    channel.set_status_provider(lambda _sid: "idle")
    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "payload": {"session_id": "s1"}}), ws
    )
    assert ws.sent[0]["type"] == "reply_snapshot"
    assert ws.sent[1]["type"] == "session/queue"
    assert ws.sent[1]["payload"]["items"][0]["placement"] == "queued"
    assert ws.sent[2]["type"] == "session/status"
    assert "frame_id" not in ws.sent[0]


@pytest.mark.asyncio
async def test_prompt_ack_waits_for_bus_reply_and_uses_request_envelope():
    bus = EventBus()
    channel = WebSocketChannel(bus)
    ws = FakeWebSocket()
    received = asyncio.create_task(channel._on_message(
        json.dumps({
            "request_id": "client-1",
            "type": "session.prompt",
            "payload": {"session_id": "s1", "mode": "queue", "content": "hello"},
        }),
        ws,
    ))
    inbound = await anext(bus.subscribe_inbound())
    assert not received.done()
    bus.resolve_inbound(inbound.id, SimpleNamespace(accepted=True, session_id="s1"))
    await received
    assert ws.sent == [{
        "request_id": "client-1",
        "ok": True,
        "value": {"accepted": True, "session_id": "s1"},
    }]


@pytest.mark.asyncio
async def test_prompt_without_request_id_is_rejected_before_bus():
    bus = EventBus()
    channel = WebSocketChannel(bus)
    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "session.prompt", "payload": {"session_id": "s", "content": "x"}}),
        ws,
    )
    assert ws.sent[0]["error"]["code"] == "missing_request_id"
    assert bus._inbound_queue.empty()


@pytest.mark.asyncio
async def test_update_queue_steer_returns_unified_ack():
    channel = WebSocketChannel(EventBus())
    channel.set_inbox_provider(_Inbox())
    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({
            "type": "session.updateQueue",
            "request_id": "update-1",
            "payload": {
                "session_id": "s1",
                "item_id": "queued-1",
                "action": {"kind": "steer"},
            },
        }),
        ws,
    )
    assert ws.sent == [{
        "request_id": "update-1",
        "ok": True,
        "value": {"accepted": True, "session_id": "s1", "item_id": "queued-1"},
    }]
