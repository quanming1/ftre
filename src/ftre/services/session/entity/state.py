"""Session 单文件快照模型。

``session.json`` 同时保存会话元信息、可恢复的 Msg 快照和请求幂等索引。
流式 Event 只存在于当前进程，不在这里逐条落盘；旧 schema 文件在读取时
一次性迁移到统一的 ``seq`` 水位。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CURRENT_SCHEMA_VERSION = 5
SUPPORTED_SCHEMA_VERSIONS = frozenset({2, 3, 4, CURRENT_SCHEMA_VERSION})


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
    """sessions/<sid>/session.json：元信息 + 完整 Msg Snapshot。"""
    # 允许未来插件在顶层增加字段；读取/写回时不主动丢弃它们。
    model_config = ConfigDict(extra="allow")

    schema_version: int = CURRENT_SCHEMA_VERSION
    session: SessionState
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Session 内 Event/Msg 共用的持久单调水位。
    seq: int = -1
    # 完整 Msg.model_dump(mode="json")，不存 assistant/chunk 记录。
    messages: list[dict[str, Any]] = Field(default_factory=list)
    # request_id → message/run/status/fingerprint，支持跨重启幂等。
    requests: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # 插件命名空间扩展数据。
    extensions: dict[str, Any] = Field(default_factory=dict)


def parse_session_meta(data: dict[str, Any]) -> SessionMetaFile:
    """校验 schema version 后解析 SessionMetaFile。"""
    if not isinstance(data, dict):
        raise ValueError("SessionMeta 必须是 JSON 对象")  # noqa: TRY004 协议边界
    normalized = dict(data)
    version = normalized.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSessionMetaVersion(version)
    if version != CURRENT_SCHEMA_VERSION:
        # v4 以前把同一概念拆成 revision/cursor；只把 cursor 迁移为
        # 持久 Event 水位，旧 revision 不再进入新模型或写回文件。
        legacy_cursor = normalized.pop("cursor", None)
        normalized.pop("revision", None)
        if "seq" not in normalized:
            normalized["seq"] = (
                int(legacy_cursor) if isinstance(legacy_cursor, (int, float)) else -1
            )
        normalized["schema_version"] = CURRENT_SCHEMA_VERSION
    else:
        # 即使 schema 已标成 v5，也不允许历史字段继续污染新快照。
        normalized.pop("revision", None)
        normalized.pop("cursor", None)
    return SessionMetaFile.model_validate(normalized)


def parse_session_meta_json(payload: str | bytes) -> SessionMetaFile:
    """从 JSON 文本解析当前版本 SessionMetaFile。"""
    return parse_session_meta(json.loads(payload))


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SessionMetaFile",
    "SessionState",
    "UnsupportedSessionMetaVersion",
    "parse_session_meta",
    "parse_session_meta_json",
]
