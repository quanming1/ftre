"""Session 的持久化状态模型。

队列已经迁移到独立的 ``ftre-inbox`` Package。这里仅保留 Session 身份、消息历史和
metadata；旧 state.json 中的 ``mailbox`` 字段只在解析时丢弃，具体迁移由 Inbox 包完成。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from ftre_agent_core.message import Msg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CURRENT_SCHEMA_VERSION = 1
_MSG_ALLOWED_KEYS = frozenset(Msg.model_fields)


def _check_msg_shape(item: Any) -> None:
    if isinstance(item, dict):
        extra = set(item) - _MSG_ALLOWED_KEYS
        if extra:
            raise ValueError(f"messages 含非 Msg 字段: {sorted(extra)}")


class UnsupportedAgentStateVersion(ValueError):
    """磁盘 state.json schema 版本超出当前代码支持范围。"""
    def __init__(self, version: Any):
        self.version = version
        super().__init__(f"不支持的 AgentState schema_version: {version!r}")


class SessionState(BaseModel):
    """state.json 中的会话元信息，不包含消息和 mailbox 内容。"""
    model_config = ConfigDict(extra="forbid")

    id: str
    agent_id: str = "default"
    channel_id: str
    title: str = ""
    workspace: str = ""
    created_at: str
    updated_at: str


class AgentStateFile(BaseModel):
    """一个 Session 的完整磁盘快照：元信息、消息和 metadata。"""
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session: SessionState
    messages: list[Msg] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_mailbox(cls, value: Any) -> Any:
        """让旧 state 可以先被 SessionService 读取，再由 Inbox 包完成迁移。"""
        if isinstance(value, dict) and "mailbox" in value:
            value = dict(value)
            value.pop("mailbox", None)
        return value

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
    """校验 schema version 和字段形状后解析 AgentStateFile。"""
    if not isinstance(data, dict):
        raise ValueError("AgentState 必须是 JSON 对象")  # noqa: TRY004 legacy compatibility boundary reviewed in F1
    version = data.get("schema_version")
    if version != CURRENT_SCHEMA_VERSION:
        raise UnsupportedAgentStateVersion(version)
    return AgentStateFile.model_validate(data)


def parse_agent_state_json(payload: str | bytes) -> AgentStateFile:
    """从 JSON 文本解析当前版本 AgentStateFile。"""
    return parse_agent_state(json.loads(payload))
