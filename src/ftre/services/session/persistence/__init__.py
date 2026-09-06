"""Storage 层：Session 数据存取（元信息 + 事件日志文件，不含业务规则）。

- json_store: session.json 元信息的原子读写引擎（~/.ftre/sessions/）
- jsonl: session.jsonl 事件日志读写 + write-behind 协调器
- repository: 基于 json_store 的 CRUD + 索引 + 提交语义
"""
from .chunk_rows import decode_storage_record, pack_chunk_runs
from .json_store import CorruptStateError, JsonStateStore, validate_session_id
from .repository import SessionRepository

__all__ = [
    "CorruptStateError",
    "JsonStateStore",
    "SessionRepository",
    "decode_storage_record",
    "pack_chunk_runs",
    "validate_session_id",
]
