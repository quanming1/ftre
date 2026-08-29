from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ftre.services.messaging.bus import EventBus, MessageBusService
from ftre.services.session.events import SessionEventService
from ftre.services.session.projection import SessionProjection
from ftre.services.session.service import SessionService


@pytest.mark.asyncio
async def test_steering_user_message_is_persisted_before_outbound_echo():
    """steer 的交接点必须先写 Session，再让客户端收到 USER_MESSAGE。"""
    order: list[str] = []
    sessions = AsyncMock()
    sessions.upsert_message.side_effect = lambda *_args: order.append("db")
    projection = SessionProjection(sessions)
    bus = AsyncMock(spec=EventBus)
    bus.publish_outbound.side_effect = lambda *_args: order.append("bus")
    service = SessionEventService(
        SimpleNamespace(projection=projection),
        MessageBusService(bus=bus),
    )

    await service.emit_user_message_if_absent(
        "session-steer",
        "ws",
        request_id="request-steer",
        content="插入下一轮",
        agent_id="default",
    )

    assert order == ["db", "bus"]
    message = sessions.upsert_message.await_args.args[1]
    assert message.role == "user"
    assert message.get_text_content() == "插入下一轮"
    assert message.metadata["request_id"] == "request-steer"
    outbound = bus.publish_outbound.await_args.args[0]
    assert outbound.type == "agent_event"
    assert outbound.data["type"] == "USER_MESSAGE"
    assert outbound.metadata.request_id == "request-steer"


@pytest.mark.asyncio
async def test_steering_retry_reuses_stable_message_id():
    """重试同一个 request_id 时交给 Session upsert 做幂等，不生成第二个消息 ID。"""
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    bus = AsyncMock(spec=EventBus)
    service = SessionEventService(
        SimpleNamespace(projection=projection),
        MessageBusService(bus=bus),
    )

    for _ in range(2):
        await service.emit_user_message_if_absent(
            "session-steer",
            "ws",
            request_id="request-same",
            content="同一条",
        )

    first = sessions.upsert_message.await_args_list[0].args[1]
    second = sessions.upsert_message.await_args_list[1].args[1]
    assert first.id == second.id
    assert first.id.startswith("user_")


@pytest.mark.asyncio
async def test_existing_user_message_keeps_identity_and_skips_rebroadcast(tmp_path):
    sessions = SessionService(sessions_dir=str(tmp_path / "sessions"))
    await sessions.init()
    session_id = await sessions.create_session("ws")
    bus = AsyncMock(spec=EventBus)
    service = SessionEventService(sessions, MessageBusService(bus=bus))

    first = await service.emit_user_message_if_absent(
        session_id,
        "ws",
        request_id="request-stable",
        content="同一条",
    )
    persisted = first.persisted_messages[0]
    second = await service.emit_user_message_if_absent(
        session_id,
        "ws",
        request_id="request-stable",
        content="同一条",
    )

    assert second.persisted_messages[0].id == persisted.id
    assert second.persisted_messages[0].created_at == persisted.created_at
    assert bus.publish_outbound.await_count == 1
    messages = await sessions.get_messages_by_session(session_id)
    assert len(messages) == 1
    assert messages[0]["created_at"] == persisted.created_at
    await sessions.close()


@pytest.mark.asyncio
async def test_existing_user_message_rejects_request_content_conflict(tmp_path):
    sessions = SessionService(sessions_dir=str(tmp_path / "sessions"))
    await sessions.init()
    session_id = await sessions.create_session("ws")
    service = SessionEventService(
        sessions,
        MessageBusService(bus=AsyncMock(spec=EventBus)),
    )

    await service.emit_user_message_if_absent(
        session_id,
        "ws",
        request_id="request-conflict",
        content="原始内容",
    )
    with pytest.raises(ValueError, match="request_id 已绑定不同内容"):
        await service.emit_user_message_if_absent(
            session_id,
            "ws",
            request_id="request-conflict",
            content="篡改内容",
        )
    await sessions.close()


@pytest.mark.asyncio
async def test_user_message_boundary_carries_run_and_previous_assistant_id():
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    bus = AsyncMock(spec=EventBus)
    service = SessionEventService(
        SimpleNamespace(projection=projection),
        MessageBusService(bus=bus),
    )

    await service.emit_user_message_if_absent(
        "session-steer",
        "ws",
        request_id="request-boundary",
        content="插入",
        run_id="turn-1",
        previous_assistant_message_id="assistant-1",
    )

    event = bus.publish_outbound.await_args.args[0]
    assert event.data["data"]["run_id"] == "turn-1"
    assert event.data["data"]["previous_assistant_message_id"] == "assistant-1"
    assert event.data["type"] == "USER_MESSAGE"


@pytest.mark.asyncio
async def test_host_pipeline_event_uses_session_topic_without_agent_projection():
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    bus = AsyncMock(spec=EventBus)
    service = SessionEventService(
        SimpleNamespace(projection=projection),
        MessageBusService(bus=bus),
    )

    await service.emit_pipeline(
        "session-1",
        "ws",
        "TURN_END",
        {"reason": ""},
        reply_id="turn-1",
    )

    event = bus.publish_outbound.await_args.args[0]
    assert event.type == "session_event"
    assert event.data.model_dump(mode="json") == {
        "type": "PIPELINE_EVENT",
        "name": "TURN_END",
        "value": {"reason": ""},
        "reply_id": "turn-1",
        "id": event.data.id,
        "created_at": event.data.created_at,
        "metadata": {},
    }
    sessions.projection.apply.assert_not_awaited()
