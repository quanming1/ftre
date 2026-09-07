"""Entity 层：Session 持久化数据模型（session.json Snapshot）。

本层只有 Pydantic 数据结构与版本校验，不含任何存储/业务逻辑。
"""
from .state import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    SessionMetaFile,
    SessionState,
    UnsupportedSessionMetaVersion,
    parse_session_meta,
    parse_session_meta_json,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SessionMetaFile",
    "SessionState",
    "UnsupportedSessionMetaVersion",
    "parse_session_meta",
    "parse_session_meta_json",
]
