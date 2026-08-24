"""Stable names and event出口 for Session maintenance events.

The projection layer owns the generic session-event contract.  Optional Feature
packages may emit these names without importing one another or reaching into the
AgentLoop implementation.
"""
# 中文说明：Session 维护事件命名和 emit sink：可选 Feature 通过它通知 Projection，不持有 AgentLoop 引用。

from enum import StrEnum
from typing import Any

from ftre.services.messaging.bus import BusMessage, EventBus, InboundMetadata


class SessionMaintenanceEvent(StrEnum):
    """CustomEvent names understood by :class:`SessionProjection`."""

    COMPACTION_START = "context_compact_start"
    COMPACTION_DONE = "context_compact_done"
    COMPACTION_FAILED = "context_compact_failed"


class SessionEventService:
    """Session 维护事件的唯一投影/广播出口。

    Service 在构造时注入 Session projection 和 MessageBus；不再由 Agent Provider
    通过 setter 临时挂接。压缩等 Package 只依赖这个稳定 ``emit`` 方法，事件会先
    持久化投影，再广播权威事实。
    """

    def __init__(self, sessions, message_bus: EventBus) -> None:
        self._sessions = sessions
        self._bus = message_bus

    async def emit(self, *args, **kwargs) -> Any:
        """先持久化 Session 事件，再广播权威事实。"""
        if len(args) < 3:
            raise TypeError("session event requires session_id, channel_id and event")
        session_id, channel_id, event = args[:3]
        metadata = kwargs.get("metadata") or InboundMetadata()
        result = await self._sessions.projection.apply(session_id, event)
        await self._bus.publish_outbound(
            BusMessage(
                type="agent_event",
                from_channel=channel_id,
                to_channel=channel_id,
                from_session=session_id,
                to_session=session_id,
                data=event.model_dump(mode="json"),
                metadata=metadata,
            )
        )
        return result


__all__ = ["SessionEventService", "SessionMaintenanceEvent"]
