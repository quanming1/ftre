"""工具注册、scoped view 和 Tool Hook 合约。"""

from .hooks import (
    TOOLS_EXECUTE_SPEC,
    TOOLS_POST_EXECUTE_SPEC,
    TOOLS_PRE_EXECUTE_SPEC,
    TOOLS_RESULT_SPEC,
)
from .service import ToolService

__all__ = [
    "TOOLS_EXECUTE_SPEC",
    "TOOLS_POST_EXECUTE_SPEC",
    "TOOLS_PRE_EXECUTE_SPEC",
    "TOOLS_RESULT_SPEC",
    "ToolService",
]
