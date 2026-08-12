"""Session 的持久化状态模型。

Mailbox 只保存尚未开始的 ``pending`` 输入；运行中的 Turn 与完成结果都属于
Gateway 内存运行态。聊天历史只写入 ``messages``，进程中断后用户可以依据已有
UserMessage 发出“继续”，但不会自动重放可能已产生副作用的工具调用。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from ftre_agent_core.message import Msg

CURRENT_SCHEMA_VERSION = 1
_MSG_ALLOWED_KEYS = frozenset(Msg.model_fields)


def _check_msg_shape(item: Any) -> None:
    if isinstance(item, dict):
        extra = set(item) - _MSG_ALLOWED_KEYS
        if extra:
            raise ValueError(f"messages 含非 Msg 字段: {sorted(extra)}")


class UnsupportedAgentStateVersion(ValueError):
    def __init__(self, version: Any):
        self.version = version
        super().__init__(f"不支持的 AgentState schema_version: {version!r}")


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    agent_id: str = "default"
    channel_id: str
    title: str = ""
    workspace: str = ""
    created_at: str
    updated_at: str


class QueueItem(BaseModel):
    """已接纳、尚未执行的用户请求。

    这是 mailbox 唯一需要写入 ``state.json`` 的业务对象。会话和 Channel
    已由它所属的 state 文件确定，Bus 信封只在进程内传递，因此不要在这里
    再复制路由、时间戳或自由 metadata。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    sequence: int
    content: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    # 仅保留执行时选择全局 agent 所需的信息；未指定时使用 default。
    agent_id: str = "default"


class MailboxState(BaseModel):
    """持久化等待队列；不记录 active 或完成历史。"""

    model_config = ConfigDict(extra="forbid")

    revision: int = 0
    next_sequence: int = 1
    pending: list[QueueItem] = Field(default_factory=list)

    @field_validator("pending")
    @classmethod
    def _validate_pending(cls, value: list[QueueItem]) -> list[QueueItem]:
        ids = [item.request_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate request_id in mailbox.pending")
        sequences = [item.sequence for item in value]
        if sequences != sorted(sequences):
            raise ValueError("mailbox.pending must be ordered by sequence")
        return value

class AgentStateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session: SessionState
    messages: list[Msg] = Field(default_factory=list)
    mailbox: MailboxState = Field(default_factory=MailboxState)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("messages", mode="before")
    @classmethod
    def reject_event_shapes(cls, value: Any) -> Any:
        if isinstance(value, list):
            for item in value:
                _check_msg_shape(item)
        return value

    @field_validator("messages", mode="after")
    @classmethod
    def validate_no_duplicate_ids(cls, value: list[Msg]) -> list[Msg]:
        ids = [m.id for m in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate Msg.id in session")
        return value


def parse_agent_state(data: dict[str, Any]) -> AgentStateFile:
    if not isinstance(data, dict):
        raise ValueError("AgentState 必须是 JSON 对象")
    version = data.get("schema_version")
    if version != CURRENT_SCHEMA_VERSION:
        raise UnsupportedAgentStateVersion(version)
    return AgentStateFile.model_validate(data)


def parse_agent_state_json(payload: str | bytes) -> AgentStateFile:
    return parse_agent_state(json.loads(payload))
