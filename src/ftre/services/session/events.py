"""Stable names and event出口 for Session maintenance events.

The projection layer owns the generic session-event contract.  Optional Feature
packages may emit these names without importing one another or reaching into the
AgentLoop implementation.
"""
# 中文说明：Session 维护事件命名和 emit sink：可选 Feature 通过它通知 Projection，不持有 AgentLoop 引用。

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from ftre_agent.event import UserMessageEvent
from ftre_agent.message import from_openai_message
from pydantic import BaseModel, ConfigDict, Field

from ftre.services.messaging.bus import (
    BusMessage,
    InboundMetadata,
    MessageBusService,
    SessionEventMessage,
)
from ftre.services.session.message.multimodal import (
    build_user_content,
    normalize_stored_user_content,
)


class SessionMaintenanceEvent(StrEnum):
    """Names of durable/in-memory session maintenance records."""

    COMPACTION_START = "context_compact_start"
    COMPACTION_DONE = "context_compact_done"
    COMPACTION_FAILED = "context_compact_failed"


def _event_id() -> str:
    return uuid.uuid4().hex[:16]


def _event_time() -> str:
    return datetime.now(UTC).isoformat()


def _request_fingerprint(content: Any, attachments: tuple[dict[str, Any], ...]) -> str:
    payload = {
        "content": content,
        "attachments": [dict(item) for item in attachments],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


class HostEventBase(BaseModel):
    """Host-owned event envelope; it never enters AgentStreamEvent."""

    model_config = ConfigDict(use_enum_values=True)
    id: str = Field(default_factory=_event_id)
    created_at: str = Field(default_factory=_event_time)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HostPipelineEvent(HostEventBase):
    """Turn/command lifecycle event consumed only by Host and Desktop."""

    type: Literal["PIPELINE_EVENT"] = "PIPELINE_EVENT"
    name: str
    value: dict[str, Any] = Field(default_factory=dict)
    reply_id: str = ""


class SessionMaintenanceRecord(HostEventBase):
    """Compaction maintenance event consumed by SessionProjection/Host UI."""

    type: Literal["SESSION_MAINTENANCE"] = "SESSION_MAINTENANCE"
    name: SessionMaintenanceEvent
    value: dict[str, Any] = Field(default_factory=dict)
    reply_id: str = ""


class SessionEventService:
    """Session 维护事件的唯一投影/广播出口。

    Service 在构造时注入 Session projection 和 MessageBus；不再由 Agent Provider
    通过 setter 临时挂接。压缩等 Package 只依赖这个稳定 ``emit`` 方法，事件会先
    持久化投影，再广播权威事实。
    """

    def __init__(self, sessions, message_bus: MessageBusService) -> None:
        self._sessions = sessions
        self._message_bus = message_bus

    async def active_assistant_message_id(self, session_id: str) -> str | None:
        """返回当前仍在聚合的 Assistant message_id，供消息边界事件引用。"""
        snapshot = await self._sessions.projection.snapshot(session_id)
        if not snapshot:
            return None
        return str(snapshot[-1].get("message_id") or snapshot[-1].get("reply_id") or "") or None

    async def emit(self, *args, **kwargs) -> Any:
        """先持久化 Session 事件，再广播权威事实。"""
        if len(args) < 3:
            raise TypeError("session event requires session_id, channel_id and event")
        session_id, channel_id, event = args[:3]
        metadata = kwargs.get("metadata") or InboundMetadata()
        if isinstance(metadata, dict):
            metadata = InboundMetadata.model_validate(metadata)
        result = await self._sessions.projection.apply(session_id, event)
        await self._message_bus.publish_outbound(
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

    async def _emit_host_event(
        self,
        session_id: str,
        channel_id: str,
        event: HostEventBase,
        *,
        metadata: InboundMetadata | dict[str, Any] | None = None,
    ) -> Any:
        """Project maintenance when needed, then broadcast a Host event."""
        if isinstance(event, SessionMaintenanceRecord):
            result = await self._sessions.projection.apply_maintenance(session_id, event)
        else:
            result = None
        wire_metadata = metadata or InboundMetadata()
        if isinstance(wire_metadata, dict):
            wire_metadata = InboundMetadata.model_validate(wire_metadata)
        await self._message_bus.publish_outbound(
            SessionEventMessage(
                from_channel=channel_id,
                to_channel=channel_id,
                from_session=session_id,
                to_session=session_id,
                data=event,
                metadata=wire_metadata,
            )
        )
        return result

    async def emit_pipeline(
        self,
        session_id: str,
        channel_id: str,
        name: str,
        value: dict[str, Any] | None = None,
        *,
        reply_id: str = "",
        metadata: InboundMetadata | dict[str, Any] | None = None,
    ) -> None:
        """Broadcast a Host pipeline event without touching Agent projection."""
        await self._emit_host_event(
            session_id,
            channel_id,
            HostPipelineEvent(name=name, value=dict(value or {}), reply_id=reply_id),
            metadata=metadata,
        )

    async def emit_maintenance(
        self,
        session_id: str,
        channel_id: str,
        name: str | SessionMaintenanceEvent,
        value: dict[str, Any] | None = None,
        *,
        reply_id: str = "",
        metadata: InboundMetadata | dict[str, Any] | None = None,
    ) -> Any:
        """Project and broadcast one typed session maintenance record."""
        await self._emit_host_event(
            session_id,
            channel_id,
            SessionMaintenanceRecord(
                name=SessionMaintenanceEvent(name),
                value=dict(value or {}),
                reply_id=reply_id,
            ),
            metadata=metadata,
        )

    async def emit_user_message_if_absent(
        self,
        session_id: str,
        channel_id: str,
        *,
        request_id: str,
        content: Any,
        attachments: tuple[dict[str, Any], ...] = (),
        source: str = "user",
        agent_id: str = "",
        run_id: str = "",
        previous_assistant_message_id: str | None = None,
    ) -> Any:
        """幂等持久化并广播一条已经被 Inbox 接纳的用户输入。

        Steering 不能先从 Inbox claim 再等待 Agent Runtime 写历史，否则进程在两步之间
        崩溃会同时丢掉 pending 和 UserMessage。这里用 ``session_id + request_id``
        生成稳定 Event/Msg id，Projection 的幂等 upsert 保证重试不重复；
        ``emit`` 仍然遵循“先落 Session、再广播 USER_MESSAGE”的统一顺序。
        """
        if not session_id or not request_id:
            raise ValueError("session_id 和 request_id 不能为空")
        digest = hashlib.sha256(
            f"{session_id}\0{request_id}".encode()
        ).hexdigest()[:24]
        event_id = f"user_{digest}"
        stored_content = normalize_stored_user_content(content)
        request_fingerprint = _request_fingerprint(stored_content, attachments)
        persisted_content = build_user_content(
            stored_content,
            [dict(item) for item in attachments],
            include_images=True,
        )
        message_metadata = {
            "hide": False,
            "request_id": request_id,
            "source": source,
            "request_fingerprint": request_fingerprint,
        }
        if agent_id:
            message_metadata["agent_id"] = agent_id
        if previous_assistant_message_id:
            message_metadata["previous_assistant_message_id"] = previous_assistant_message_id
        event = UserMessageEvent(
            id=event_id,
            reply_id=run_id or f"input_{digest[:16]}",
            content=from_openai_message(
                {"role": "user", "content": persisted_content}
            ),
            message_metadata=message_metadata,
            data={
                "session_id": session_id,
                "request_id": request_id,
                "content": content,
                "attachments": [dict(item) for item in attachments],
                "source": source,
                "run_id": run_id,
                "previous_assistant_message_id": previous_assistant_message_id,
            },
        )
        get_state_message = getattr(self._sessions, "get_state_message", None)
        if callable(get_state_message):
            existing_raw = await get_state_message(session_id, event_id)
            if isinstance(existing_raw, dict):
                from ftre.services.session.message.converter import _as_msg
                from ftre.services.session.projection import ProjectionResult

                existing = _as_msg(existing_raw)
                existing_metadata = existing.metadata or {}
                existing_fingerprint = existing_metadata.get("request_fingerprint")
                if existing_fingerprint and existing_fingerprint != request_fingerprint:
                    raise ValueError(
                        f"request_id 已绑定不同内容: {request_id}"
                    )
                if not existing_fingerprint and existing.content != event.content:
                    raise ValueError(
                        f"request_id 已绑定不同内容: {request_id}"
                    )
                return ProjectionResult(persisted_messages=[existing])
        return await self.emit(
            session_id,
            channel_id,
            event,
            metadata=InboundMetadata(request_id=request_id, agent_id=agent_id),
        )


__all__ = [
    "HostEventBase",
    "HostPipelineEvent",
    "SessionEventService",
    "SessionMaintenanceEvent",
    "SessionMaintenanceRecord",
]
