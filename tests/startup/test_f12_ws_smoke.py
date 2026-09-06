"""WebSocket 协议 smoke：不依赖真实 LLM 或桌面客户端（PRD-F41 S 场景）。

帧形状契约：attach 基线 = session/subscribed + session/queue（两帧）；
prompt/updateQueue/cancel 结算 = rpc 帧 {v, session_id, type:"rpc", payload}。
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ftre.plugins.builtin.channels.websocket.channel import WebSocketChannel
from ftre.services.messaging.bus import EventBus


class FakeInbox:
    def __init__(self) -> None:
        self.item = SimpleNamespace(request_id="queued-1")
        self.placement = "queued"
        self.content = "queued"
        self.revision = 1
        self.edited: list[tuple[str, str]] = []
        self.removed: list[str] = []

    async def wire_snapshot(self, session_id: str) -> dict:
        return {
            "session_id": session_id,
            "revision": self.revision,
            "items": [] if self.item is None else [{
                "id": self.item.request_id,
                "placement": self.placement,
                "message": {"content": [{"type": "text", "text": self.content}]},
            }],
        }

    async def edit(self, _session_id, item_id, content, _attachments):
        self.edited.append((item_id, content))
        self.content = content
        self.revision += 1
        return True

    async def remove(self, _session_id, item_id):
        self.removed.append(item_id)
        self.item = None
        self.revision += 1
        return True

    async def promote(self, _session_id, _item_id):
        self.placement = "steering"
        self.revision += 1
        return True

    async def snapshot(self, _session_id):
        return SimpleNamespace(next_turn=(self.item,), next_step=())


class FakeSessions:
    async def last_seq(self, session_id: str) -> int:
        return 5


def test_real_websocket_attach_prompt_queue_mutation_cancel_and_reconnect():
    bus = EventBus()
    inbound_modes: list[tuple[str, str]] = []

    async def request_inbound(message):
        if message.type == "user_message":
            inbound_modes.append((message.metadata.request_id, message.data.get("mode", "")))
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
        sessions_service=FakeSessions(),
        status_provider=lambda _session_id: "idle",
    )

    with TestClient(channel.app) as client:
        with client.websocket_connect("/") as websocket:
            websocket.send_json({
                "type": "attach",
                "payload": {"session_id": "s1"},
            })
            # 基线两连：subscribed{last_seq,status} → queue 快照
            baseline = [websocket.receive_json() for _ in range(2)]
            assert [frame["type"] for frame in baseline] == [
                "session/subscribed", "session/queue",
            ]
            assert baseline[0]["v"] == 1
            assert baseline[0]["session_id"] == "s1"
            assert baseline[0]["payload"]["last_seq"] == 5
            assert baseline[0]["payload"]["status"] == "idle"
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
            prompt_response = websocket.receive_json()
            assert prompt_response["type"] == "rpc"
            assert prompt_response["payload"]["request_id"] == "prompt-1"
            assert prompt_response["payload"]["ok"] is True
            assert prompt_response["payload"]["value"]["session_id"] == "s1"
            assert "revision" in prompt_response["payload"]["value"]
            assert inbound_modes == [("prompt-1", "queue")]

            websocket.send_json({
                "type": "session.updateQueue",
                "request_id": "edit-1",
                "payload": {
                    "session_id": "s1",
                    "item_id": "queued-1",
                    "action": {"kind": "edit", "content": "edited"},
                },
            })
            edit_response = websocket.receive_json()
            assert edit_response["type"] == "rpc"
            assert edit_response["payload"]["ok"] is True
            assert edit_response["payload"]["value"]["items"][0]["message"]["content"][0]["text"] == "edited"
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
            steer_response = websocket.receive_json()
            assert steer_response["type"] == "rpc"
            assert steer_response["payload"]["request_id"] == "steer-1"
            assert steer_response["payload"]["ok"] is True
            assert inbound_modes[-1] == ("steer-1", "steer")

            websocket.send_json({
                "type": "session.updateQueue",
                "request_id": "remove-1",
                "payload": {
                    "session_id": "s1",
                    "item_id": "queued-1",
                    "action": {"kind": "remove"},
                },
            })
            remove_response = websocket.receive_json()
            assert remove_response["type"] == "rpc"
            assert remove_response["payload"]["value"]["items"] == []
            assert inbox.removed == ["queued-1"]

            websocket.send_json({
                "type": "session.cancel",
                "request_id": "cancel-1",
                "payload": {"session_id": "s1"},
            })
            cancel_response = websocket.receive_json()
            assert cancel_response["type"] == "rpc"
            assert cancel_response["payload"]["request_id"] == "cancel-1"
            assert cancel_response["payload"]["ok"] is True
            assert cancel_response["payload"]["value"]["accepted"] is True

        # 新连接再次 attach，仍能获得完整权威 baseline（多端同步语义）。
        with client.websocket_connect("/") as websocket:
            websocket.send_json({
                "type": "attach",
                "payload": {"session_id": "s1"},
            })
            reconnect = [websocket.receive_json() for _ in range(2)]
            assert reconnect[0]["type"] == "session/subscribed"
            assert reconnect[1]["type"] == "session/queue"
            assert reconnect[1]["payload"]["session_id"] == "s1"
