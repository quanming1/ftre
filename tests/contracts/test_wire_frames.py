"""下行 wire 帧 golden 契约测试（PRD-F41 §4.4 / F43 附录 B）。

6 种帧逐一构造合法 payload，断言帧信封 {v, session_id, type, payload}
与 dump 形状；帧级无 seq（事件 seq 为唯一权威）。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from ftre.services.messaging.bus import EventBus
from ftre.services.messaging.wire import (
    RpcFrame,
    SessionEventFrame,
    SessionMaintenanceFrame,
    SessionProjectionFrame,
    SessionQueueFrame,
    SessionSubscribedFrame,
)

FRAMES = [
    (
        SessionEventFrame,
        {"event": {"type": "turn/start", "seq": 0, "time": 1, "message_id": None, "data": {"turn_id": "t1"}}},
        "session/event",
    ),
    (SessionSubscribedFrame, {"seq": 7, "events": [], "status": "idle", "has_more": False, "resync_required": False}, "session/subscribed"),
    (SessionQueueFrame, {"revision": 3, "items": []}, "session/queue"),
    (SessionProjectionFrame, {"key": "token_usage", "value": {"total_tokens": 1}, "seq": 12}, "session/projection"),
    (SessionMaintenanceFrame, {"name": "command_message", "value": {"content": "已执行 /compact", "level": "info", "request_id": "r1"}}, "session/maintenance"),
    (RpcFrame, {"request_id": "r1", "ok": True, "value": {"accepted": True}}, "rpc"),
]


@pytest.mark.parametrize(("frame_cls", "payload", "frame_type"), FRAMES)
def test_frame_envelope_shape(frame_cls, payload, frame_type):
    frame = frame_cls(session_id="ws_sess_golden", payload=payload)
    dumped = frame.model_dump(mode="json")
    assert dumped == {"v": 1, "session_id": "ws_sess_golden", "type": frame_type, "payload": payload}
    assert json.loads(json.dumps(dumped, ensure_ascii=False)) == dumped


def test_frame_rejects_extra_fields():
    with pytest.raises(ValidationError):
        SessionEventFrame(session_id="s", payload={}, unexpected=1)


def test_frame_version_is_frozen():
    with pytest.raises(ValidationError):
        SessionSubscribedFrame(v=2, session_id="s", payload={"seq": 0, "events": []})


# ── 双投 byte-identical（PRD-F41 FR5 / AC5；F43 AC8）─────────────────


class _RecordingChannel:
    """桩 Channel：记录收到的每条 BusMessage 并 dump 成 wire bytes。"""

    def __init__(self, channel_id: str):
        self.channel_id = channel_id
        self.sent: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg) -> None:
        import json

        self.sent.append(json.dumps(msg.data, ensure_ascii=False, sort_keys=True))


@pytest.mark.asyncio
async def test_downstream_frame_dual_delivery_is_byte_identical():
    from ftre.services.messaging.channel.manager import ChannelManager

    bus = EventBus()
    manager = ChannelManager(bus)
    octo_channel = _RecordingChannel("octo")
    ws_channel = _RecordingChannel("ws")
    manager.register(octo_channel)
    manager.register(ws_channel)
    await manager.start()

    from ftre.services.messaging.bus.service import MessageBusService

    service = MessageBusService(bus)
    frame = SessionEventFrame(
        session_id="ws_sess_dual",
        payload={"event": {"type": "turn/start", "seq": 3, "time": 1, "message_id": None,
                           "data": {"turn_id": "t1", "trigger": "user"}}},
    )
    await service.publish_frame("ws_sess_dual", "octo", frame)
    for _ in range(50):
        if octo_channel.sent and ws_channel.sent:
            break
        await asyncio.sleep(0.01)

    # owner channel 与 ws 观察面各收到同一帧，bytes 完全一致
    assert octo_channel.sent == ws_channel.sent
    assert len(octo_channel.sent) == 1
    assert '"turn/start"' in octo_channel.sent[0]
    await manager.stop()
