"""Session 身份、消息持久化、投影和生命周期 Hook。"""

from .events import (
    HostPipelineEvent,
    SessionEventService,
    SessionMaintenanceEvent,
    SessionMaintenanceRecord,
)
from .hooks import (
    SESSION_CREATED_SPEC,
    SESSION_DISPOSED_SPEC,
)
from .service import SessionService

__all__ = [
    "SESSION_CREATED_SPEC",
    "SESSION_DISPOSED_SPEC",
    "HostPipelineEvent",
    "SessionEventService",
    "SessionMaintenanceEvent",
    "SessionMaintenanceRecord",
    "SessionService",
]
