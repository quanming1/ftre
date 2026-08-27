"""Inbox 的最小持久化模型。

``next_turn`` 和 ``next_step`` 是 Inbox 内部概念，不能泄漏到 AgentService。
客户端只接收由 Package 计算出的 placement 视图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

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

    def to_json(self, target: QueueTarget) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "content": self.content,
            "attachments": [dict(item) for item in self.attachments],
            "source": self.source,
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
            "schema_version": 1,
            "session_id": self.session_id,
            "revision": self.revision,
            "next_sequence": self.next_sequence,
            "next_turn": [item.to_json("next-turn") for item in self.next_turn],
            "next_step": [item.to_json("next-step") for item in self.next_step],
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> _MutableInbox:
        if value.get("schema_version") != 1:
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
        for items in (self.next_turn, self.next_step):
            sequences = [item.sequence for item in items]
            if sequences != sorted(sequences):
                raise ValueError("Inbox 每条队列必须按 sequence 有序")
        if len({item.sequence for item in all_items}) != len(all_items):
            raise ValueError("Inbox sequence 重复")
