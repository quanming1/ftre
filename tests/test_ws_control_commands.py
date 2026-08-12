"""WebSocket 控制指令透传测试。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ftre.bus import EventBus
from ftre.channel.ws_channel import WebSocketChannel


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.application_state = 1

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


@pytest.mark.asyncio
async def test_allow_command_is_a_plain_user_message():
    """通道层不解析工具确认，只透传不入库控制指令。"""
    channel = WebSocketChannel(EventBus())
    received = asyncio.create_task(channel._on_message(
        json.dumps({
            "type": "user_message",
            "frame_id": "f1",
            "data": {
                "session_id": "ws_sess_test",
                "content": "/allow call_1 call_2",
            },
        }),
        FakeWebSocket(),
    ))
    message = await anext(channel.bus.subscribe_inbound())
    channel.bus.resolve_inbound(
        message.id,
        SimpleNamespace(
            accepted=True, session_id="ws_sess_test", request_id="request-1",
            queue_position=1, created=True,
        ),
    )
    await received

    assert message.type == "user_message"
    assert message.data["content"] == "/allow call_1 call_2"
    assert message.metadata.request_id == "f1"


@pytest.mark.asyncio
async def test_cancel_is_a_control_message_not_a_queued_user_message():
    """停止按钮不能再伪造 /cancel 文本并在刷新后出现在消息队列。"""
    channel = WebSocketChannel(EventBus())
    ws = FakeWebSocket()
    received = asyncio.create_task(channel._on_message(
        json.dumps({
            "type": "cancel",
            "frame_id": "stop-1",
            "data": {
                "session_id": "ws_sess_test",
                "expected_request_id": "request-running",
            },
        }),
        ws,
    ))
    message = await anext(channel.bus.subscribe_inbound())
    assert message.type == "turn_cancel"
    assert message.data == {
        "session_id": "ws_sess_test",
        "expected_request_id": "request-running",
    }
    channel.bus.resolve_inbound(
        message.id,
        SimpleNamespace(accepted=True, session_id="ws_sess_test", created=True),
    )
    await received

    assert ws.sent[-1]["type"] == "control_ack"
    assert ws.sent[-1]["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_legacy_confirmation_frame_is_ignored():
    """旧的专用确认帧不再属于 WebSocket 协议。"""
    channel = WebSocketChannel(EventBus())
    published: list = []
    await channel._on_message(
        json.dumps({
            "type": "user_confirm_result",
            "data": {
                "session_id": "ws_sess_test",
                "reply_id": "reply_abc",
                "tool_call_id": "call_1",
                "approved": True,
            },
        }),
        FakeWebSocket(),
    )

    assert published == []
    assert channel.bus._inbound_queue.empty()
