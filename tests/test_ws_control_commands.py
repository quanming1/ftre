"""现代 WebSocket 输入协议：Command 仍在接入层旁路，Cancel 不伪装成消息。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ftre.services.messaging.bus import EventBus
from ftre.services.messaging.channel.providers.websocket.channel import WebSocketChannel


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.application_state = 1

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


@pytest.mark.asyncio
async def test_prompt_command_text_is_forwarded_to_command_plane():
    channel = WebSocketChannel(EventBus())
    ws = FakeWebSocket()
    received = asyncio.create_task(channel._on_message(
        json.dumps({
            "type": "session.prompt",
            "request_id": "f1",
            "payload": {"session_id": "s1", "mode": "queue", "content": "/allow call_1"},
        }),
        ws,
    ))
    message = await anext(channel.bus.subscribe_inbound())
    channel.bus.resolve_inbound(message.id, SimpleNamespace(accepted=True, session_id="s1"))
    await received
    assert message.type == "user_message"
    assert message.data["content"] == "/allow call_1"
    assert message.metadata.request_id == "f1"
    assert ws.sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_session_cancel_is_control_message_not_inbox_input():
    channel = WebSocketChannel(EventBus())
    ws = FakeWebSocket()
    received = asyncio.create_task(channel._on_message(
        json.dumps({
            "type": "session.cancel",
            "request_id": "stop-1",
            "payload": {"session_id": "s1", "expected_request_id": "running"},
        }),
        ws,
    ))
    message = await anext(channel.bus.subscribe_inbound())
    assert message.type == "turn_cancel"
    channel.bus.resolve_inbound(message.id, SimpleNamespace(created=True))
    await received
    assert ws.sent[-1] == {
        "request_id": "stop-1",
        "ok": True,
        "value": {"accepted": True, "session_id": "s1"},
    }


@pytest.mark.asyncio
async def test_legacy_confirmation_frame_is_ignored():
    channel = WebSocketChannel(EventBus())
    await channel._on_message(
        json.dumps({"type": "user_confirm_result", "payload": {"session_id": "s1"}}),
        FakeWebSocket(),
    )
    assert channel.bus._inbound_queue.empty()
