from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ftre.services.messaging.bus import EventBus, MessageBusService
from ftre.services.session.events import SessionEventService
from ftre.services.session.projection import SessionProjection


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
