"""Inbox 的最小持久化模型。

``next_turn`` 和 ``next_step`` 是 Inbox 内部概念，不能泄漏到 AgentService。
客户端只接收由 Package 计算出的 placement 视图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ftre_agent.message import Msg

QueueTarget = Literal["next-turn", "next-step"]
QueueSource = Literal["user", "plugin", "system"]


@dataclass(frozen=True, slots=True)
class QueueItem:
    """一条仍在 Inbox 中、尚未交付给 AgentService 的输入。"""

    request_id: str
    sequence: int
    session_id: str
    channel_id: str
    content: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    source: QueueSource = "user"
    # Steering 在 Hook 前已由 SessionProjection 持久化时，保存同一 UserMsg id，
    # 让 idle fallback 进入独立 Turn 时不重复写历史。该字段不改变 Queue 协议。
    history_message_id: str | None = None
    # AgentService 数据面使用 Msg[]；content/attachments 仅是旧线协议的投影。
    messages: tuple[Msg, ...] = ()
    # Inbox 的 Agent identity 与 Runtime 内部 profile agent_id 可以不同。
    agent_id: str = "default"

    def normalized_messages(self) -> tuple[Msg, ...]:
        """返回至少包含一条消息的执行输入。"""
        if self.messages:
            return self.messages
        from ftre_agent.message import UserMsg

        return (UserMsg(content=self.content, metadata={"request_id": self.request_id}),)

    def to_json(self, target: QueueTarget) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "content": self.content,
            "attachments": [dict(item) for item in self.attachments],
            "source": self.source,
            "agent_id": self.agent_id,
            "messages": [message.model_dump(mode="json") for message in self.messages],
            "target": target,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> tuple[QueueItem, QueueTarget]:
        target = value.get("target")
        if target not in {"next-turn", "next-step"}:
            raise ValueError(f"未知 Inbox target: {target!r}")
        source = value.get("source", "user")
        if source not in {"user", "plugin", "system"}:
            raise ValueError(f"未知 Inbox source: {source!r}")
        raw_messages = value.get("messages", ())
        if not isinstance(raw_messages, list):
            raise TypeError("messages 必须是数组")
        from ftre_agent.message import Msg

        messages = tuple(Msg.model_validate(item) for item in raw_messages)
        return cls(
            request_id=str(value["request_id"]),
            sequence=int(value["sequence"]),
            session_id=str(value["session_id"]),
            channel_id=str(value.get("channel_id", "")),
            content=str(value.get("content", "")),
            attachments=tuple(dict(item) for item in value.get("attachments", ())),
            source=source,
            history_message_id=(
                str(value["history_message_id"])
                if value.get("history_message_id")
                else None
            ),
            messages=messages,
            agent_id=str(value.get("agent_id") or "default"),
        ), target


@dataclass(frozen=True, slots=True)
class InboxSnapshot:
    """某个 Session 的完整 pending 快照。"""

    session_id: str
    revision: int
    next_sequence: int
    next_turn: tuple[QueueItem, ...] = ()
    next_step: tuple[QueueItem, ...] = ()
    capacity: int = 100
    inflight_count: int = 0

    @property
    def pending(self) -> tuple[QueueItem, ...]:
        """返回按 sequence 合并的只读视图。"""
        return tuple(sorted((*self.next_turn, *self.next_step), key=lambda item: item.sequence))

    @property
    def has_pending(self) -> bool:
        return bool(self.next_turn or self.next_step)


@dataclass(slots=True)
class _MutableInbox:
    """Repository 内部可变状态；不从 Service API 暴露。"""

    session_id: str
    revision: int = 0
    next_sequence: int = 1
    next_turn: list[QueueItem] = field(default_factory=list)
    next_step: list[QueueItem] = field(default_factory=list)
    inflight: dict[str, LeaseRecord] = field(default_factory=dict)

    def snapshot(self, capacity: int) -> InboxSnapshot:
        return InboxSnapshot(
            session_id=self.session_id,
            revision=self.revision,
            next_sequence=self.next_sequence,
            next_turn=tuple(self.next_turn),
            next_step=tuple(self.next_step),
            capacity=capacity,
            inflight_count=len(self.inflight),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "session_id": self.session_id,
            "revision": self.revision,
            "next_sequence": self.next_sequence,
            "next_turn": [item.to_json("next-turn") for item in self.next_turn],
            "next_step": [item.to_json("next-step") for item in self.next_step],
            "inflight": [lease.to_json() for lease in self.inflight.values()],
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> _MutableInbox:
        if value.get("schema_version") not in {1, 2}:
            raise ValueError(f"不支持的 Inbox schema_version: {value.get('schema_version')!r}")
        state = cls(
            session_id=str(value["session_id"]),
            revision=int(value.get("revision", 0)),
            next_sequence=int(value.get("next_sequence", 1)),
        )
        for raw in value.get("next_turn", ()):
            item, target = QueueItem.from_json(raw)
            if target != "next-turn":
                raise ValueError("next_turn 含有错误 target")
            state.next_turn.append(item)
        for raw in value.get("next_step", ()):
            item, target = QueueItem.from_json(raw)
            if target != "next-step":
                raise ValueError("next_step 含有错误 target")
            state.next_step.append(item)
        raw_inflight = value.get("inflight", ())
        if not isinstance(raw_inflight, list):
            raise TypeError("inflight 必须是数组")
        for raw in raw_inflight:
            lease = LeaseRecord.from_json(raw)
            if lease.item.request_id in state.inflight:
                raise ValueError("Inbox inflight request_id 重复")
            state.inflight[lease.item.request_id] = lease
        state.validate()
        return state

    def validate(self) -> None:
        all_items = [*self.next_turn, *self.next_step]
        ids = [item.request_id for item in all_items]
        if len(ids) != len(set(ids)):
            raise ValueError("Inbox 中 request_id 重复")
        for items in (self.next_turn, self.next_step):
            sequences = [item.sequence for item in items]
            if sequences != sorted(sequences):
                raise ValueError("Inbox 每条队列必须按 sequence 有序")
        if len({item.sequence for item in all_items}) != len(all_items):
            raise ValueError("Inbox sequence 重复")
        inflight_ids = set(self.inflight)
        if inflight_ids.intersection(ids):
            raise ValueError("Inbox pending/inflight request_id 重复")
        for request_id, lease in self.inflight.items():
            if request_id != lease.item.request_id or lease.item.session_id != self.session_id:
                raise ValueError("Inbox inflight item 归属错误")


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    """一次持久化 claim 的所有权记录，正常失败可释放，重启时不重投。"""

    lease_id: str
    owner_id: str
    target: QueueTarget
    item: QueueItem
    expires_at: datetime
    attempt: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "owner_id": self.owner_id,
            "target": self.target,
            "item": self.item.to_json(self.target),
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "attempt": self.attempt,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> LeaseRecord:
        if not isinstance(value, dict):
            raise TypeError("inflight 项必须是对象")
        item_value = value.get("item")
        if not isinstance(item_value, dict):
            raise TypeError("inflight.item 必须是对象")
        item, target = QueueItem.from_json(item_value)
        if value.get("target") != target:
            raise ValueError("inflight target 与 item 不一致")
        expires_at = datetime.fromisoformat(str(value["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return cls(
            lease_id=str(value["lease_id"]),
            owner_id=str(value.get("owner_id") or ""),
            target=target,
            item=item,
            expires_at=expires_at.astimezone(UTC),
            attempt=max(1, int(value.get("attempt", 1))),
        )


__all__ = ["InboxSnapshot", "LeaseRecord", "QueueItem", "QueueSource", "QueueTarget"]
