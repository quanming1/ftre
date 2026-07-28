"""reply_snapshot attach 协议测试（替代旧 volatile replay 测试）。

设计文档：docs/running-reply-snapshot-resume-design.md §5.4
"""
import json
from unittest.mock import AsyncMock

import pytest

from ftre.bus import BusMessage, EventBus
from ftre.channel.ws_channel import WebSocketChannel


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.application_state = 1  # WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


@pytest.mark.asyncio
async def test_attach_no_active_replies_sends_nothing():
    """没有进行中 Reply 时，attach 不发送 reply_snapshot。"""
    channel = WebSocketChannel(EventBus())
    channel._reply_projection = AsyncMock()
    channel._reply_projection.snapshot = AsyncMock(return_value=[])

    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "data": {"session_id": "ws_sess_test"}}),
        ws,
    )

    assert len(ws.sent) == 0


@pytest.mark.asyncio
async def test_attach_with_active_reply_sends_snapshot():
    """有进行中 Reply 时，attach 发送 reply_snapshot 帧。"""
    channel = WebSocketChannel(EventBus())
    channel._reply_projection = AsyncMock()
    channel._reply_projection.snapshot = AsyncMock(return_value=[
        {
            "reply_id": "reply_abc",
            "revision": 5,
            "message": {"id": "reply_abc", "role": "assistant", "content": []},
        }
    ])

    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "data": {"session_id": "ws_sess_test"}}),
        ws,
    )

    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["type"] == "reply_snapshot"
    assert frame["data"]["session_id"] == "ws_sess_test"
    assert len(frame["data"]["replies"]) == 1
    assert frame["data"]["replies"][0]["reply_id"] == "reply_abc"
    assert frame["data"]["replies"][0]["revision"] == 5
