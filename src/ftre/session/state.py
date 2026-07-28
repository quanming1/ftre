"""Agent State 持久化 Schema（schema_version=1）。

每个 Session 一份 state.json，根结构只有五个字段：

    schema_version / session / messages / summary / metadata

约束：
- messages[] 每项必须是完整 Msg（不持久化流式 Event）；
- summary.message 必须是 SystemMsg，且不放入 messages；
- summary.through_message_id 必须引用 messages 中真实存在的 Msg；
- 磁盘 JSON 结构版本由 schema_version 表示，未知版本拒绝加载。

JSON Schema 由 Pydantic 生成，不手工维护第二份：

    schema = AgentStateFile.model_json_schema()
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ftre_agent_core.message import Msg

CURRENT_SCHEMA_VERSION = 1

# Msg 允许出现的顶层键；磁盘 messages[] 中不允许额外字段，
# 防止 Event 类型 / Event data / reply_id 列等流式结构混入持久化状态。
_MSG_ALLOWED_KEYS = frozenset(Msg.model_fields)


def _check_msg_shape(item: Any) -> None:
    if isinstance(item, dict):
        extra = set(item) - _MSG_ALLOWED_KEYS
        if extra:
            raise ValueError(
                f"messages 含非 Msg 字段（疑似 Event 混入）: {sorted(extra)}"
            )


class UnsupportedAgentStateVersion(ValueError):
    """磁盘 state.json 的 schema_version 不被当前代码理解。"""

    def __init__(self, version: Any):
        self.version = version
        super().__init__(f"不支持的 AgentState schema_version: {version!r}")


class SessionState(BaseModel):
    """会话元信息（running/idle 等运行态不写入文件）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    agent_id: str = "default"
    channel_id: str
    title: str = ""
    workspace: str = ""
    created_at: str  # ISO 8601
    updated_at: str  # ISO 8601


class SummaryState(BaseModel):
    """当前有效的滚动摘要（不保存历史版本）。"""

    model_config = ConfigDict(extra="forbid")

    message: Msg
    through_message_id: str

    @field_validator("message", mode="before")
    @classmethod
    def reject_event_shape(cls, value: Any) -> Any:
        _check_msg_shape(value)
        return value

    @model_validator(mode="after")
    def validate_summary_message(self) -> "SummaryState":
        if self.message.role != "system":
            raise ValueError("summary.message must be a SystemMsg")
        return self


class AgentStateFile(BaseModel):
    """单个 Session 的完整持久化状态。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session: SessionState
    messages: list[Msg] = Field(default_factory=list)
    summary: SummaryState | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("messages", mode="before")
    @classmethod
    def reject_event_shapes(cls, value: Any) -> Any:
        if isinstance(value, list):
            for item in value:
                _check_msg_shape(item)
        return value

    @model_validator(mode="after")
    def validate_references(self) -> "AgentStateFile":
        ids = [message.id for message in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate Msg.id in session")
        if (
            self.summary is not None
            and self.summary.through_message_id not in set(ids)
        ):
            raise ValueError("summary cursor does not reference a message")
        return self


def parse_agent_state(data: dict[str, Any]) -> AgentStateFile:
    """按 schema_version 加载磁盘 JSON；未知版本明确拒绝。

    旧程序不得覆盖新格式数据，因此更高版本直接抛
    UnsupportedAgentStateVersion，而不是尽力解析。
    """
    if not isinstance(data, dict):
        raise ValueError("AgentState 必须是 JSON 对象")
    version = data.get("schema_version")
    if version == CURRENT_SCHEMA_VERSION:
        return AgentStateFile.model_validate(data)
    raise UnsupportedAgentStateVersion(version)


def parse_agent_state_json(payload: str | bytes) -> AgentStateFile:
    """从 JSON 文本加载 AgentState。"""
    return parse_agent_state(json.loads(payload))
