"""Inbox 的持久模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ftre_agent.message import Msg

QueueTarget = Literal["next-turn", "next-step"]
QueueSource = Literal["user", "plugin", "system"]


@dataclass(frozen=True, slots=True)
class QueueItem:
    """一条已接纳、尚未交给 AgentService 的输入。"""

    request_id: str
    sequence: int
    session_id: str
    channel_id: str
    content: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    source: QueueSource = "user"
    history_message_id: str | None = None
    messages: tuple[Msg, ...] = ()
    agent_id: str = "default"
    target_run_id: str | None = None

    def normalized_messages(self) -> tuple[Msg, ...]:
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
            "history_message_id": self.history_message_id,
            "messages": [message.model_dump(mode="json") for message in self.messages],
            "agent_id": self.agent_id,
            "target_run_id": self.target_run_id,
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
            messages=tuple(Msg.model_validate(item) for item in raw_messages),
            agent_id=str(value.get("agent_id") or "default"),
            target_run_id=(
                str(value["target_run_id"])
                if value.get("target_run_id")
                else None
            ),
        ), target


@dataclass(frozen=True, slots=True)
class InboxSnapshot:
    """某个 Session 的 pending 快照。"""

    session_id: str
    revision: int
    next_sequence: int
    next_turn: tuple[QueueItem, ...] = ()
    next_step: tuple[QueueItem, ...] = ()
    capacity: int = 100

    @property
    def pending(self) -> tuple[QueueItem, ...]:
        return tuple(sorted((*self.next_turn, *self.next_step), key=lambda item: item.sequence))

    @property
    def has_pending(self) -> bool:
        return bool(self.next_turn or self.next_step)

@dataclass(slots=True)
class _MutableInbox:
    session_id: str
    revision: int = 0
    next_sequence: int = 1
    next_turn: list[QueueItem] = field(default_factory=list)
    next_step: list[QueueItem] = field(default_factory=list)

    def snapshot(self, capacity: int) -> InboxSnapshot:
        return InboxSnapshot(
            session_id=self.session_id,
            revision=self.revision,
            next_sequence=self.next_sequence,
            next_turn=tuple(self.next_turn),
            next_step=tuple(self.next_step),
            capacity=capacity,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 3,
            "session_id": self.session_id,
            "revision": self.revision,
            "next_sequence": self.next_sequence,
            "next_turn": [item.to_json("next-turn") for item in self.next_turn],
            "next_step": [item.to_json("next-step") for item in self.next_step],
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> _MutableInbox:
        if value.get("schema_version") not in {1, 2, 3}:
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
        state.validate()
        return state

    def validate(self) -> None:
        all_items = [*self.next_turn, *self.next_step]
        ids = [item.request_id for item in all_items]
        if len(ids) != len(set(ids)):
            raise ValueError("Inbox 中 request_id 重复")
        if len({item.sequence for item in all_items}) != len(all_items):
            raise ValueError("Inbox sequence 重复")
        for items in (self.next_turn, self.next_step):
            if [item.sequence for item in items] != sorted(item.sequence for item in items):
                raise ValueError("Inbox 每条队列必须按 sequence 有序")


__all__ = ["InboxSnapshot", "QueueItem", "QueueSource", "QueueTarget"]
