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
        data={"type": event_type, "id": f"ev_{event_type}", "data": data or {}},
    )


@pytest.mark.asyncio
async def test_attach_replays_volatile_events_buffered_without_subscribers():
    channel = WebSocketChannel(EventBus())
    session_id = "ws::sess_volatile"

    await channel.send(_agent_event(session_id, "TEXT_BLOCK_DELTA", {"delta": "hello"}))

    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "data": {"session_id": session_id}}),
        ws,
    )

    assert len(ws.sent) == 1
    replay = ws.sent[0]
    assert replay["type"] == "agent_event"
    assert replay["data"] == {
        "type": "TEXT_BLOCK_DELTA",
        "id": "ev_TEXT_BLOCK_DELTA",
        "data": {"delta": "hello"},
    }
    assert replay["metadata"]["session_id"] == session_id
    assert not any(key.startswith("volatile") for key in replay["metadata"])
    assert "volatile" not in replay["metadata"]
    assert "volatile_epoch" not in replay["metadata"]
    assert "replay" not in replay["metadata"]


@pytest.mark.asyncio
async def test_persisted_complete_replaces_assistant_volatile_replay():
    channel = WebSocketChannel(EventBus())
    session_id = "ws::sess_done"

    await channel.send(_agent_event(session_id, "TEXT_BLOCK_DELTA", {"delta": "draft"}))
    await channel.send(
        _agent_event(session_id, "REPLY_END", {"content": "final"})
    )

    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "data": {"session_id": session_id}}),
        ws,
    )

    # REPLY_END is not a volatile event type, so it's not buffered.
    # The TEXT_BLOCK_DELTA was cleared by REPLY_END.
    assert len(ws.sent) == 0


@pytest.mark.asyncio
async def test_attach_replays_context_compact_start_while_running():
    channel = WebSocketChannel(EventBus())
    session_id = "ws::sess_compacting"

    await channel.send(
        _agent_event(session_id, "context_compact_start", {"events": 12, "tokens": 50000})
    )

    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "data": {"session_id": session_id}}),
        ws,
    )

    assert len(ws.sent) == 1
    assert ws.sent[0]["data"] == {
        "type": "context_compact_start",
        "id": "ev_context_compact_start",
        "data": {"events": 12, "tokens": 50000},
    }


@pytest.mark.asyncio
async def test_context_compact_done_clears_compact_start_replay():
    channel = WebSocketChannel(EventBus())
    session_id = "ws::sess_compact_done"

    await channel.send(
        _agent_event(session_id, "context_compact_start", {"events": 12, "tokens": 50000})
    )
    await channel.send(
        _agent_event(session_id, "context_compact_done", {"summary": "done"})
    )

    ws = FakeWebSocket()
    await channel._on_message(
        json.dumps({"type": "attach", "data": {"session_id": session_id}}),
        ws,
    )

    assert len(ws.sent) == 0
