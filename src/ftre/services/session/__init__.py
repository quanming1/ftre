"""Session 身份、事件日志持久化和生命周期 Hook。"""

from .hooks import (
    SESSION_CREATED_SPEC,
    SESSION_DISPOSED_SPEC,
)
from .service import SessionService

__all__ = [
    "SESSION_CREATED_SPEC",
    "SESSION_DISPOSED_SPEC",
    "SessionService",
]
