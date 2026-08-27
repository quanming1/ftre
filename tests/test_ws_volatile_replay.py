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
        self.placement = "queued"
        self.target = "next-turn"
        self.revision = 1

    async def wire_snapshot(self, session_id):
        return {
            "session_id": session_id,
            "revision": self.revision,
            "items": [{
                "id": self.item.request_id,
                "placement": self.placement,
                "message": {"content": []},
            }],
        }

    async def snapshot(self, _session_id):
        return SimpleNamespace(
            next_turn=(self.item,) if self.target == "next-turn" else (),
            next_step=(self.item,) if self.target == "next-step" else (),
        )

    async def promote(self, _session_id, _item_id):
        self.placement = "steering"
        self.target = "next-step"
        self.revision += 1
        return True

    async def edit(self, _session_id, _item_id, _content, _attachments):
        return True

    async def remove(self, _session_id, _item_id):
        return True


@pytest.mark.asyncio
async def test_attach_reads_inbox_queue_and_status_baseline():
    projection = SimpleNamespace(
        snapshot=AsyncMock(return_value=[]),
        session_event_snapshot=AsyncMock(return_value=[]),
    )
    channel = WebSocketChannel(
        EventBus(),
        session_projection=projection,
        inbox_provider=_Inbox(),
        status_provider=lambda _sid: "idle",
    )
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
async def test_prompt_response_waits_for_bus_reply_and_uses_queue_envelope():
    bus = EventBus()
    channel = WebSocketChannel(bus, inbox_provider=_Inbox())
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
        "type": "session/queue",
        "request_id": "client-1",
        "ok": True,
        "payload": {
            "session_id": "s1",
            "revision": 1,
            "items": [{"id": "queued-1", "placement": "queued", "message": {"content": []}}],
        },
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
async def test_update_queue_steer_returns_latest_queue_snapshot():
    channel = WebSocketChannel(EventBus(), inbox_provider=_Inbox())
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
        "type": "session/queue",
        "request_id": "update-1",
        "ok": True,
        "payload": {
            "session_id": "s1",
            "revision": 2,
            "items": [{"id": "queued-1", "placement": "steering", "message": {"content": []}}],
        },
    }]


@pytest.mark.asyncio
async def test_steering_item_is_immutable_until_claim():
    """steering 已进入下一次 Reasoning 的交接区，不能被并发 edit/remove。"""
    channel = WebSocketChannel(EventBus(), inbox_provider=_Inbox())
    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({
            "type": "session.updateQueue",
            "request_id": "steer-1",
            "payload": {
                "session_id": "s1",
                "item_id": "queued-1",
                "action": {"kind": "steer"},
            },
        }),
        ws,
    )
    await channel._on_message(
        json.dumps({
            "type": "session.updateQueue",
            "request_id": "edit-locked",
            "payload": {
                "session_id": "s1",
                "item_id": "queued-1",
                "action": {"kind": "edit", "content": "不应修改"},
            },
        }),
        ws,
    )
    assert ws.sent[-1]["ok"] is False
    assert ws.sent[-1]["error"]["code"] == "steering-locked"
