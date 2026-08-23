"""MCP Feature 公共导出：暴露连接管理和 MCP Service，不把底层传输对象泄露给其他 Feature。"""

from .connection import McpConnection, McpManager
from .service import McpService

__all__ = ["McpConnection", "McpManager", "McpService"]
