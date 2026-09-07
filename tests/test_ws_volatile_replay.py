"""WebSocket attach 基线与 rpc 结算（模板见 tests/startup/test_f12_ws_smoke.py）。

wire 帧形状（PRD-F41）：
- attach 基线两连：session/subscribed{seq, events, status} → session/queue 快照；
- prompt/updateQueue 结算 = rpc 帧 {v, session_id, type:"rpc",
  payload:{request_id, ok, value?|error?}}。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ftre.plugins.builtin.channels.websocket.channel import WebSocketChannel
from ftre.services.messaging.bus import EventBus
from ftre.services.messaging.wire import SessionQueueFrame, SessionSubscribedFrame


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


def _baseline(inbox):
    async def provider(session_id, **_kwargs):
        return [
            SessionSubscribedFrame(
                session_id=session_id,
                payload={"seq": 5, "events": [], "status": "idle", "has_more": False},
            ).model_dump(mode="json"),
            SessionQueueFrame(
                session_id=session_id,
                payload=await inbox.wire_snapshot(session_id),
            ).model_dump(mode="json"),
        ]

    return provider


def _control(inbox):
    async def handler(_frame_type, _frame, data):
        session_id = data["session_id"]
        item_id = data["item_id"]
        action = data["action"]
        snapshot = await inbox.snapshot(session_id)
        steering_ids = {item.request_id for item in snapshot.next_step}
        if action["kind"] in {"edit", "remove"} and item_id in steering_ids:
            return {"ok": False, "error": {"code": "steering-locked"}}
        if action["kind"] == "steer":
            await inbox.promote(session_id, item_id)
        elif action["kind"] == "edit":
            await inbox.edit(session_id, item_id, action["content"], None)
        elif action["kind"] == "remove":
            await inbox.remove(session_id, item_id)
        return {"ok": True, "value": await inbox.wire_snapshot(session_id)}

    return handler


@pytest.mark.asyncio
async def test_attach_reads_inbox_queue_and_status_baseline():
    channel = WebSocketChannel(
        EventBus(),
        baseline_provider=_baseline(_Inbox()),
    )
    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "payload": {"session_id": "s1"}}), ws
    )
    # 基线两连：subscribed{seq, events, status} → queue 快照
    assert len(ws.sent) == 2
    assert ws.sent[0]["type"] == "session/subscribed"
    assert ws.sent[0]["v"] == 1
    assert ws.sent[0]["session_id"] == "s1"
    assert ws.sent[0]["payload"]["seq"] == 5
    assert ws.sent[0]["payload"]["status"] == "idle"
    assert ws.sent[1]["type"] == "session/queue"
    assert ws.sent[1]["payload"]["items"][0]["placement"] == "queued"
    assert "frame_id" not in ws.sent[0]


@pytest.mark.asyncio
async def test_prompt_response_waits_for_bus_reply_and_uses_queue_envelope():
    bus = EventBus()
    inbox = _Inbox()
    channel = WebSocketChannel(bus, snapshot_provider=inbox.wire_snapshot)
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
        "v": 1,
        "session_id": "s1",
        "type": "rpc",
        "payload": {
            "request_id": "client-1",
            "ok": True,
            "value": {
                "session_id": "s1",
                "revision": 1,
                "items": [{"id": "queued-1", "placement": "queued", "message": {"content": []}}],
            },
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
    # 拒绝也是 rpc 帧（payload.error）
    assert ws.sent[0]["type"] == "rpc"
    assert ws.sent[0]["payload"]["ok"] is False
    assert ws.sent[0]["payload"]["error"]["code"] == "missing_request_id"
    assert bus._inbound_queue.empty()


@pytest.mark.asyncio
async def test_update_queue_steer_returns_latest_queue_snapshot():
    inbox = _Inbox()
    channel = WebSocketChannel(EventBus(), control_handler=_control(inbox))
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
        "v": 1,
        "session_id": "s1",
        "type": "rpc",
        "payload": {
            "request_id": "update-1",
            "ok": True,
            "value": {
                "session_id": "s1",
                "revision": 2,
                "items": [{"id": "queued-1", "placement": "steering", "message": {"content": []}}],
            },
        },
    }]


@pytest.mark.asyncio
async def test_steering_item_is_immutable_until_claim():
    """steering 已进入下一次 Reasoning 的交接区，不能被并发 edit/remove。"""
    inbox = _Inbox()
    channel = WebSocketChannel(EventBus(), control_handler=_control(inbox))
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
    # 拒绝也是 rpc 帧（payload.ok=False + payload.error）
    assert ws.sent[-1]["type"] == "rpc"
    assert ws.sent[-1]["payload"]["ok"] is False
    assert ws.sent[-1]["payload"]["error"]["code"] == "steering-locked"
