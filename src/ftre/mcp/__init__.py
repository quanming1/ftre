"""
MCP (Model Context Protocol) 集成模块

提供 MCP 服务器连接管理和工具注册能力，让 ftre Agent 可以调用
外部 MCP 服务器提供的工具（如 filesystem、github、browser 等）。

核心组件：
- McpConfig: 配置解析（从 ~/.ftre/config.json 的 mcp 段读取）
- McpManager: 连接管理（connect/disconnect/listTools/reconnect）
- McpToolAdapter: MCP tool → ftre Tool 转换

参考 OpenCode 的 MCP 集成设计，简化去掉 OAuth 部分（ftre 当前只支持 Local stdio）。
"""
from .manager import McpManager

__all__ = ["McpManager"]