"""Storage 层：Session 数据存取（只认 AgentStateFile，不含业务规则）。

- json_store: ~/.ftre/sessions/ 目录的原子读写引擎
- repository: 基于 json_store 的 CRUD + 索引 + 提交语义
"""
from .json_store import CorruptStateError, JsonStateStore, validate_session_id
from .repository import SessionRepository

__all__ = [
    "CorruptStateError",
    "JsonStateStore",
    "SessionRepository",
    "validate_session_id",
]
