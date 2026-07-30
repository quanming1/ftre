"""ws_channel user_confirm_result 帧解析测试。

前端收到 REQUIRE_USER_CONFIRM 后回传用户决定，ws_channel 将其转为
kind="user_confirm_result" 的 BusMessage 投递到 Bus，驱动 Agent 从挂起恢复。
"""
import json

import pytest

from ftre.bus import EventBus
from ftre.channel.ws_channel import WebSocketChannel


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.application_state = 1  # WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _make_channel():
    channel = WebSocketChannel(EventBus())
    published: list = []

    async def _capture(msg):
        published.append(msg)

    channel.bus.publish_inbound = _capture  # type: ignore[method-assign]
    return channel, published


@pytest.mark.asyncio
async def test_user_confirm_result_publishes_bus_message():
    """合法 user_confirm_result 帧转为对应 kind 的 BusMessage。"""
    channel, published = _make_channel()
    ws = FakeWebSocket()

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
        ws,
    )

    assert len(published) == 1
    msg = published[0]
    assert msg.type == "user_confirm_result"
    assert msg.to_session == "ws_sess_test"
    assert msg.data["reply_id"] == "reply_abc"
    assert msg.data["tool_call_id"] == "call_1"
    assert msg.data["approved"] is True


@pytest.mark.asyncio
async def test_user_confirm_result_rejects_missing_fields():
    """缺少 tool_call_id / 非布尔 approved 时拒绝，不投递 BusMessage。"""
    channel, published = _make_channel()
    ws = FakeWebSocket()

    await channel._on_message(
        json.dumps({
            "type": "user_confirm_result",
            "frame_id": "f1",
            "data": {
                "session_id": "ws_sess_test",
                "reply_id": "reply_abc",
                "approved": "yes",
            },
        }),
        ws,
    )

    assert len(published) == 0
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["data"]["code"] == "invalid_input"


@pytest.mark.asyncio
async def test_user_confirm_result_ignored_without_session_id():
    """缺少 session_id 时忽略，不投递也不回 reject。"""
    channel, published = _make_channel()
    ws = FakeWebSocket()

    await channel._on_message(
        json.dumps({
            "type": "user_confirm_result",
            "data": {
                "reply_id": "reply_abc",
                "tool_call_id": "call_1",
                "approved": False,
            },
        }),
        ws,
    )

    assert len(published) == 0
    assert len(ws.sent) == 0
