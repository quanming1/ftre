"""F12 后端 WebSocket 协议 smoke：不依赖真实 LLM 或桌面客户端。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ftre.plugins.builtin.channels.websocket.channel import WebSocketChannel
from ftre.services.messaging.bus import EventBus


class FakeInbox:
    def __init__(self) -> None:
        self.item = SimpleNamespace(request_id="queued-1")
        self.edited: list[tuple[str, str]] = []
        self.removed: list[str] = []

    async def wire_snapshot(self, session_id: str) -> dict:
        return {
            "session_id": session_id,
            "items": [{
                "id": self.item.request_id,
                "placement": "queued",
                "message": {"content": [{"type": "text", "text": "queued"}]},
            }],
        }

    async def edit(self, _session_id, item_id, content, _attachments):
        self.edited.append((item_id, content))
        return True

    async def remove(self, _session_id, item_id):
        self.removed.append(item_id)
        return True

    async def promote(self, _session_id, _item_id):
        return True

    async def snapshot(self, _session_id):
        return SimpleNamespace(next_turn=(self.item,), next_step=())


def test_real_websocket_attach_prompt_queue_mutation_cancel_and_reconnect():
    bus = EventBus()

    async def request_inbound(message):
        if message.type == "turn_cancel":
            return SimpleNamespace(created=True)
        return SimpleNamespace(
            accepted=True,
            session_id=message.to_session,
            request_id=message.metadata.request_id,
        )

    bus.request_inbound = request_inbound
    inbox = FakeInbox()
    channel = WebSocketChannel(
        bus,
        app=FastAPI(title="ftre-test"),
        inbox_provider=inbox,
        status_provider=lambda _session_id: "idle",
    )

    with TestClient(channel.app) as client:
        with client.websocket_connect("/") as websocket:
            websocket.send_json({
                "type": "attach",
                "payload": {"session_id": "s1"},
            })
            baseline = [websocket.receive_json() for _ in range(3)]
            assert [frame["type"] for frame in baseline] == [
                "reply_snapshot", "session/queue", "session/status",
            ]
            assert baseline[1]["payload"]["items"][0]["placement"] == "queued"

            websocket.send_json({
                "type": "session.prompt",
                "request_id": "prompt-1",
                "payload": {
                    "session_id": "s1",
                    "mode": "queue",
                    "content": "hello",
                },
            })
            assert websocket.receive_json() == {
                "request_id": "prompt-1",
                "ok": True,
                "value": {"accepted": True, "session_id": "s1"},
            }

            websocket.send_json({
                "type": "session.updateQueue",
                "request_id": "edit-1",
                "payload": {
                    "session_id": "s1",
                    "item_id": "queued-1",
                    "action": {"kind": "edit", "content": "edited"},
                },
            })
            assert websocket.receive_json()["ok"] is True
            assert inbox.edited == [("queued-1", "edited")]

            websocket.send_json({
                "type": "session.prompt",
                "request_id": "steer-1",
                "payload": {
                    "session_id": "s1",
                    "mode": "steer",
                    "content": "请改用中文",
                },
            })
            assert websocket.receive_json() == {
                "request_id": "steer-1",
                "ok": True,
                "value": {"accepted": True, "session_id": "s1"},
            }

            websocket.send_json({
                "type": "session.updateQueue",
                "request_id": "remove-1",
                "payload": {
                    "session_id": "s1",
                    "item_id": "queued-1",
                    "action": {"kind": "remove"},
                },
            })
            assert websocket.receive_json()["ok"] is True
            assert inbox.removed == ["queued-1"]

            websocket.send_json({
                "type": "session.cancel",
                "request_id": "cancel-1",
                "payload": {"session_id": "s1"},
            })
            assert websocket.receive_json() == {
                "request_id": "cancel-1",
                "ok": True,
                "value": {"accepted": True, "session_id": "s1"},
            }

        # 新连接再次 attach，仍能获得完整权威 baseline，而不是依赖旧连接状态。
        with client.websocket_connect("/") as websocket:
            websocket.send_json({
                "type": "attach",
                "payload": {"session_id": "s1"},
            })
            reconnect = [websocket.receive_json() for _ in range(3)]
            assert reconnect[1]["type"] == "session/queue"
            assert reconnect[1]["payload"]["session_id"] == "s1"
