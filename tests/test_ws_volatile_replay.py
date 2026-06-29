import json

import pytest

from ftre.bus import BusMessage, EventBus
from ftre.channel.ws_channel import WebSocketChannel


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _agent_event(session_id: str, event_type: str, data: dict | None = None) -> BusMessage:
    return BusMessage(
        type="agent_event",
        from_channel="agent",
        to_channel="ws",
        from_session=session_id,
        to_session=session_id,
        data={"type": event_type, "data": data or {}},
    )


@pytest.mark.asyncio
async def test_attach_replays_volatile_events_buffered_without_subscribers():
    channel = WebSocketChannel(EventBus())
    session_id = "ws::sess_volatile"

    await channel.send(_agent_event(session_id, "assistant_message", {"content": "hello"}))

    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "data": {"session_id": session_id}}),
        ws,
    )

    assert len(ws.sent) == 1
    replay = ws.sent[0]
    assert replay["type"] == "agent_event"
    assert replay["data"] == {"type": "assistant_message", "data": {"content": "hello"}}
    assert replay["metadata"]["session_id"] == session_id
    assert replay["metadata"]["volatile_seq"] == 1
    assert "volatile" not in replay["metadata"]
    assert "volatile_epoch" not in replay["metadata"]
    assert "replay" not in replay["metadata"]


@pytest.mark.asyncio
async def test_persisted_complete_clears_assistant_volatile_replay():
    channel = WebSocketChannel(EventBus())
    session_id = "ws::sess_done"

    await channel.send(_agent_event(session_id, "assistant_message", {"content": "draft"}))
    await channel.send(
        _agent_event(session_id, "assistant_message_complete", {"content": "final"})
    )

    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "data": {"session_id": session_id}}),
        ws,
    )

    assert ws.sent == []
