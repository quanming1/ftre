"""
MCP Tool → ftre Tool 适配器

将 MCP 服务器提供的工具转换为 ftre_agent_core.tool.Tool 实例，
使其可以像内置工具（bash / read / write 等）一样注册到 ToolRegistry。

工具名规则：mcp__{server_name}__{tool_name}
  - 例如：mcp__filesystem__read_file
  - 前缀 mcp__ 避免与内置工具命名冲突
  - 双下划线分隔服务器名和工具名

MCP tool schema → ftre ToolParameter 映射：
  string   → string
  number   → number
  integer  → number
  boolean  → boolean
  array    → string（JSON 序列化提示）
  object   → string（JSON 序列化提示）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ftre_agent_core.tool import Tool, ToolParameter
from mcp import Tool as McpToolDef

from .manager import McpManager

logger = logging.getLogger(__name__)

# MCP schema type → ftre ToolParameter type
_TYPE_MAP = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
}

# 工具名前缀，用于识别 MCP 工具
MCP_TOOL_PREFIX = "mcp__"


def mcp_tool_id(server_name: str, tool_name: str) -> str:
    """生成 ftre 工具名：mcp__{server}__{tool}"""
    return f"{MCP_TOOL_PREFIX}{server_name}__{tool_name}"


def _parse_tool_name(tool_id: str) -> tuple[str, str] | None:
    """从 ftre 工具名解析出 (server_name, mcp_tool_name)"""
    if not tool_id.startswith(MCP_TOOL_PREFIX):
        return None
    rest = tool_id[len(MCP_TOOL_PREFIX):]
    parts = rest.split("__", 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _convert_parameters(mcp_tool: McpToolDef) -> list[ToolParameter]:
    """将 MCP tool 的 inputSchema 转换为 ftre ToolParameter 列表"""
    schema = mcp_tool.inputSchema
    if not isinstance(schema, dict):
        return []

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    params: list[ToolParameter] = []
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue

        prop_type = prop.get("type", "string")
        ftre_type = _TYPE_MAP.get(prop_type, "string")

        # array / object 类型用 string + JSON 提示
        description = prop.get("description", "")
        if prop_type == "array":
            if description:
                description += "（JSON 数组格式）"
            else:
                description = "JSON 数组格式"
        elif prop_type == "object":
            if description:
                description += "（JSON 对象格式）"
            else:
                description = "JSON 对象格式"

        # enum 透传
        enum = prop.get("enum")

        params.append(ToolParameter(
            name=name,
            type=ftre_type,
            description=description,
            required=name in required,
            enum=enum,
        ))

    return params


def create_mcp_tool(
    server_name: str,
    mcp_tool: McpToolDef,
    manager: McpManager,
) -> Tool:
    """将单个 MCP 工具转换为 ftre Tool 实例

    Args:
        server_name: MCP 服务器名
        mcp_tool: MCP 协议返回的工具定义
        manager: McpManager 实例，用于 call_tool
    """
    tool_id = mcp_tool_id(server_name, mcp_tool.name)
    parameters = _convert_parameters(mcp_tool)
    description = mcp_tool.description or f"MCP tool: {mcp_tool.name}"

    async def _execute(**kwargs) -> str:
        """异步调用 MCP 工具，返回文本结果"""
        try:
            result = await manager.call_tool(server_name, mcp_tool.name, kwargs)
            # MCP callTool 返回 CallToolResult，含 content 列表
            content_list = getattr(result, "content", [])
            if not content_list:
                return ""

            parts: list[str] = []
            for item in content_list:
                item_type = getattr(item, "type", "")
                if item_type == "text":
                    parts.append(getattr(item, "text", ""))
                elif item_type == "image":
                    parts.append("[image]")
                elif item_type == "resource":
                    resource = getattr(item, "resource", None)
                    if resource and hasattr(resource, "text"):
                        parts.append(resource.text)
                    elif resource and hasattr(resource, "blob"):
                        parts.append("[resource blob]")
                    else:
                        parts.append("[resource]")
                else:
                    parts.append(str(item))

            return "\n".join(parts)

        except Exception as e:
            logger.error(f"[mcp] call_tool 失败: {tool_id} — {e}")
            return f"[MCP 错误] {type(e).__name__}: {e}"

    return Tool(
        name=tool_id,
        description=description,
        parameters=parameters,
        func=_execute,
    )


async def build_mcp_tools(manager: McpManager) -> list[Tool]:
    """从所有已连接的 MCP 服务器发现并转换工具

    Returns:
        ftre Tool 实例列表，可直接注册到 ToolRegistry
    """
    all_mcp_tools = await manager.list_all_tools()
    if not all_mcp_tools:
        return []

    tools: list[Tool] = []
    for server_name, mcp_tool in all_mcp_tools:
        try:
            tool = create_mcp_tool(server_name, mcp_tool, manager)
            tools.append(tool)
            logger.info(f"[mcp] 注册工具: {tool.name} (来自 {server_name})")
        except Exception as e:
            logger.warning(f"[mcp] 工具转换失败: {server_name}/{mcp_tool.name} — {e}")

    return tools
