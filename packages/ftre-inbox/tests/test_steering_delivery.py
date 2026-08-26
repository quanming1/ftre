from __future__ import annotations

import pytest
from ftre_inbox.protocol import InboundMessage
from ftre_inbox.repository import InboxRepository
from ftre_inbox.service import InboxService

from ftre.services.messaging.bus import BusMessage, InboundData


class RecordingSessionEvents:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def emit_user_message_if_absent(self, *args, **kwargs):
        del args, kwargs
        self.order.append("persist")


class FailingSessionEvents:
    async def emit_user_message_if_absent(self, *args, **kwargs):
        del args, kwargs
        raise RuntimeError("session store unavailable")


@pytest.mark.asyncio
async def test_plugin_inject_is_not_written_to_user_history(tmp_path) -> None:
    order: list[str] = []
    service = InboxService(
        InboxRepository(tmp_path),
        session_events=RecordingSessionEvents(order),
    )
    await service.inject(InboundMessage("s1", "plugin-1", "plugin", "内部上下文", source="plugin"))

    claimed = await service.deliver_next_step_for_reasoning("s1")

    assert [item.request_id for item in claimed] == ["plugin-1"]
    assert order == []
    await service.close()


@pytest.mark.asyncio
async def test_duplicate_steering_request_keeps_one_pending_item(tmp_path) -> None:
    seen: set[str] = set()
    service = InboxService(
        InboxRepository(tmp_path, request_seen=lambda _session, request_id: request_id in seen),
    )

    first = await service.steer(InboundMessage("s1", "same", "ws", "第一次"))
    assert first.created is True
    seen.add("same")
    second = await service.steer(InboundMessage("s1", "same", "ws", "重发"))

    assert second.accepted is True
    assert second.created is False
    snapshot = await service.snapshot("s1")
    assert [(item.request_id, item.content) for item in snapshot.next_step] == [("same", "第一次")]
    await service.close()


def test_inbound_data_preserves_prompt_mode() -> None:
    data = InboundData.coerce({
        "session_id": "s1",
        "content": "steer",
        "mode": "steer",
    })

    assert data.mode == "steer"
    assert data.model_dump()["mode"] == "steer"
    assert InboundData.coerce({"content": "legacy"}).mode == "queue"


@pytest.mark.asyncio
async def test_bus_rejects_unknown_prompt_mode_instead_of_downgrading(tmp_path) -> None:
    service = InboxService(InboxRepository(tmp_path))
    result = await service.handle_bus_message(
        BusMessage(
            type="user_message",
            from_channel="ws",
            from_session="s1",
            to_channel="ws",
            to_session="s1",
            data={"session_id": "s1", "content": "bad", "mode": "later"},
        )
    )

    assert result.accepted is False
    assert result.error == {
        "code": "invalid_mode",
        "message": "mode 只能是 queue 或 steer",
        "retryable": False,
    }
    assert not (await service.snapshot("s1")).has_pending
    await service.close()


@pytest.mark.asyncio
async def test_bus_mode_routes_to_next_step(tmp_path) -> None:
    service = InboxService(InboxRepository(tmp_path))
    result = await service.handle_bus_message(
        BusMessage(
            type="user_message",
            from_channel="ws",
            from_session="s1",
            to_channel="ws",
            to_session="s1",
            data={
                "session_id": "s1",
                "content": "steer",
                "mode": "steer",
            },
        )
    )

    assert result.accepted is True
    snapshot = await service.snapshot("s1")
    assert [item.request_id for item in snapshot.next_step] == [result.request_id]
    assert snapshot.next_turn == ()
    await service.close()


@pytest.mark.asyncio
async def test_steering_persists_before_claim(tmp_path) -> None:
    order: list[str] = []
    repository = InboxRepository(tmp_path)
    service = InboxService(
        repository,
        session_events=RecordingSessionEvents(order),
    )
    await service.steer(InboundMessage("s1", "r1", "ws", "steer"))

    original_claim = repository.claim

    async def claim(*args, **kwargs):
        order.append("claim")
        return await original_claim(*args, **kwargs)

    repository.claim = claim
    claimed = await service.deliver_next_step_for_reasoning("s1")

    assert [item.request_id for item in claimed] == ["r1"]
    assert order == ["persist", "claim"]
    assert not (await service.snapshot("s1")).has_pending
    await service.close()


@pytest.mark.asyncio
async def test_steering_history_failure_keeps_pending(tmp_path) -> None:
    service = InboxService(
        InboxRepository(tmp_path),
        session_events=FailingSessionEvents(),
    )
    await service.steer(InboundMessage("s1", "r1", "ws", "steer"))

    with pytest.raises(RuntimeError, match="session store unavailable"):
        await service.deliver_next_step_for_reasoning("s1")

    snapshot = await service.snapshot("s1")
    assert [item.request_id for item in snapshot.next_step] == ["r1"]
    await service.close()


@pytest.mark.asyncio
async def test_promoted_plugin_message_becomes_persisted_user_input(tmp_path) -> None:
    order: list[str] = []
    service = InboxService(
        InboxRepository(tmp_path),
        session_events=RecordingSessionEvents(order),
    )
    await service.followup(
        InboundMessage("s1", "cron-1", "cron", "来自定时任务", source="plugin")
    )

    assert await service.promote("s1", "cron-1") is True
    promoted = await service.snapshot("s1")
    assert promoted.next_step[0].source == "user"

    claimed = await service.deliver_next_step_for_reasoning("s1")

    assert [item.request_id for item in claimed] == ["cron-1"]
    assert order == ["persist"]
    assert not (await service.snapshot("s1")).has_pending
    await service.close()
