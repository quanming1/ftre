"""WebSocket 控制指令透传测试。"""
import json

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
    published: list = []

    async def _capture(message):
        published.append(message)

    channel.bus.publish_inbound = _capture  # type: ignore[method-assign]
    await channel._on_message(
        json.dumps({
            "type": "user_message",
            "frame_id": "f1",
            "data": {
                "session_id": "ws_sess_test",
                "content": "/allow call_1 call_2",
            },
        }),
        FakeWebSocket(),
    )

    assert len(published) == 1
    message = published[0]
    assert message.type == "user_message"
    assert message.data["content"] == "/allow call_1 call_2"
    assert message.metadata["frame_id"] == "f1"


@pytest.mark.asyncio
async def test_legacy_confirmation_frame_is_ignored():
    """旧的专用确认帧不再属于 WebSocket 协议。"""
    channel = WebSocketChannel(EventBus())
    published: list = []

    async def _capture(message):
        published.append(message)

    channel.bus.publish_inbound = _capture  # type: ignore[method-assign]
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
