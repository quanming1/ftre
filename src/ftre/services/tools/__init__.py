"""工具注册、scoped view 和 Tool Hook 合约。"""

from .hooks import (
    TOOL_AFTER_SPEC,
    TOOL_BEFORE_SPEC,
)
from .service import ToolService

__all__ = [
    "TOOL_AFTER_SPEC",
    "TOOL_BEFORE_SPEC",
    "ToolService",
]
