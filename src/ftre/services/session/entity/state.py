"""Session 元信息持久化模型（session.json，PRD-F43）。

架构（PRD-F43）：消息事实位于 session.jsonl 事件日志；session.json 只保留
会话身份、metadata 与 external 绑定。last_user_text 反规范化存储（会话列表预览，
由 SessionService 在 user/message 事件时更新），避免列表页全量 derive。
旧格式会话目录启动时直接忽略：不解析、不迁移。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CURRENT_SCHEMA_VERSION = 2


class UnsupportedSessionMetaVersion(ValueError):
    """磁盘 session.json schema 版本超出当前代码支持范围。"""

    def __init__(self, version: Any):
        self.version = version
        super().__init__(f"不支持的 session.json schema_version: {version!r}")


class SessionState(BaseModel):
    """会话身份与展示信息。"""
    model_config = ConfigDict(extra="forbid")

    id: str
    agent_id: str = "default"
    channel_id: str
    title: str = ""
    workspace: str = ""
    created_at: str
    updated_at: str
    # 会话列表预览：最后一条真实用户消息摘要（反规范化，随 user/message 更新）
    last_user_text: str = ""


class SessionMetaFile(BaseModel):
    """sessions/<sid>/session.json：元信息 + metadata（无消息）。"""
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    session: SessionState
    metadata: dict[str, Any] = Field(default_factory=dict)


def parse_session_meta(data: dict[str, Any]) -> SessionMetaFile:
    """校验 schema version 后解析 SessionMetaFile。"""
    if not isinstance(data, dict):
        raise ValueError("SessionMeta 必须是 JSON 对象")  # noqa: TRY004 协议边界
    version = data.get("schema_version")
    if version != CURRENT_SCHEMA_VERSION:
        raise UnsupportedSessionMetaVersion(version)
    return SessionMetaFile.model_validate(data)


def parse_session_meta_json(payload: str | bytes) -> SessionMetaFile:
    """从 JSON 文本解析当前版本 SessionMetaFile。"""
    return parse_session_meta(json.loads(payload))


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "SessionMetaFile",
    "SessionState",
    "UnsupportedSessionMetaVersion",
    "parse_session_meta",
    "parse_session_meta_json",
]
