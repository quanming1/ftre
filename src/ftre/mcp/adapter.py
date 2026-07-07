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
import base64
import json
import logging
from typing import Any

from ftre_agent_core.agent.event import UserMessageEvent, user_message_event
from ftre_agent_core.tool import Tool, ToolParameter
from mcp import Tool as McpToolDef

from ftre.utils.image_store import save_image

from .manager import McpManager

logger = logging.getLogger(__name__)

# MCP schema type → ftre ToolParameter type
_TYPE_MAP = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
}

# 工具名前缀：MCP 工具统一以 mcp__ 开头，便于与内置工具区分
MCP_TOOL_PREFIX = "mcp__"


def mcp_tool_id(server_name: str, tool_name: str) -> str:
    """生成 ftre 侧的 MCP 工具名。

    命名格式固定为 `mcp__{server}__{tool}`，这样在工具注册表里能
    一眼看出这个工具来自哪个 MCP 服务器，同时避免和内置工具重名。
    """
    return f"{MCP_TOOL_PREFIX}{server_name}__{tool_name}"


def _parse_tool_name(tool_id: str) -> tuple[str, str] | None:
    """从 ftre 工具名里反推出 MCP 的 server 和 tool 名。

    这里只做轻量解析，不做严格校验；如果格式不符合预期，直接返回 None。
    """
    if not tool_id.startswith(MCP_TOOL_PREFIX):
        return None
    rest = tool_id[len(MCP_TOOL_PREFIX):]
    parts = rest.split("__", 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _convert_parameters(mcp_tool: McpToolDef) -> list[ToolParameter]:
    """把 MCP tool 的 JSON Schema 输入定义，转换成 ftre 的参数定义。

    这里采用“尽量保真 + 适度降级”的策略：
    - string / number / integer / boolean 直接映射
    - array / object 降级成 string，并在说明里提示用户用 JSON 格式传值
    - enum 原样透传，保留前端和模型侧的枚举约束
    """
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

        # array / object 类型在 ftre 里没有独立参数结构，统一转成 string。
        # 为了减少调用方困惑，在描述中明确告诉模型和用户要传 JSON。
        description = prop.get("description", "")
        if prop_type == "array":
            description = f"{description}（JSON 数组格式）" if description else "JSON 数组格式"
        elif prop_type == "object":
            description = f"{description}（JSON 对象格式）" if description else "JSON 对象格式"

        # MCP schema 里的 enum 约束可以直接沿用，方便前端展示和模型约束。
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

    async def _execute(**kwargs) -> str | UserMessageEvent:
        """执行单个 MCP 工具调用。

        返回策略：
        - 纯文本结果：直接拼成字符串返回，保持和普通工具一致
        - 包含图片结果：把图片落盘后返回 UserMessageEvent，让下游走视觉消息链路
        - 文本 + 图片同时出现时：图片优先，文本放到 metadata 里，避免信息丢失
        """
        try:
            result = await manager.call_tool(server_name, mcp_tool.name, kwargs)
            # MCP callTool 返回 CallToolResult，content 是一个 block 列表，里面可能混有 text / image / resource。
            content_list = getattr(result, "content", [])
            if not content_list:
                return ""

            text_parts: list[str] = []
            image_event: UserMessageEvent | None = None

            for item in content_list:
                item_type = getattr(item, "type", "")

                if item_type == "text":
                    text = getattr(item, "text", "")
                    if text:
                        text_parts.append(text)
                    continue

                if item_type == "image":
                    # MCP 的 image block 里带 base64 数据；ftre 内部要先落盘，再通过 image_file 事件下发。
                    data = getattr(item, "data", "")
                    mime_type = getattr(item, "mimeType", "image/png")
                    if not data:
                        text_parts.append("[image]")
                        continue

                    try:
                        image_bytes = base64.b64decode(data)
                        stored_path = save_image(
                            image_bytes,
                            mime_type,
                            original_name=f"{server_name}_{mcp_tool.name}.png",
                        )
                        image_event = user_message_event(
                            content=[{
                                "type": "image_file",
                                "path": stored_path,
                                "mime_type": mime_type,
                            }],
                            metadata={
                                # 这条消息是工具返回的中间视觉结果，不需要直接展示给用户。
                                "hide": True,
                                "mime": mime_type,
                                "size": len(image_bytes),
                            },
                        )
                    except Exception as image_error:
                        logger.warning(f"[mcp] 图片内容处理失败: {tool_id} — {image_error}")
                        text_parts.append("[image]")
                    continue

                if item_type == "resource":
                    # resource 可能是文本资源，也可能是二进制 blob。
                    # 二进制内容不直接塞进 prompt，先落盘保存，再给 AI 一个可点击/可读取的路径提示。
                    resource = getattr(item, "resource", None)
                    if resource and hasattr(resource, "text"):
                        text = resource.text
                        if text:
                            text_parts.append(text)
                    elif resource and hasattr(resource, "blob"):
                        try:
                            blob_bytes = base64.b64decode(resource.blob)
                            original_name = getattr(resource, "name", "") or f"{server_name}_{mcp_tool.name}"
                            stored_path = save_image(blob_bytes, getattr(resource, "mimeType", "application/octet-stream"), original_name=original_name)
                            text_parts.append(
                                f"[resource 二进制内容已保存到本地，请读取该路径获取完整内容: {stored_path}]"
                            )
                        except Exception as blob_error:
                            logger.warning(f"[mcp] resource blob 处理失败: {tool_id} — {blob_error}")
                            text_parts.append("[resource blob]")
                    else:
                        text_parts.append("[resource]")
                    continue

                # 未知 block 类型统一转字符串，保证工具不会因为协议扩展直接失败。
                text_parts.append(str(item))

            text_output = "\n".join(part for part in text_parts if part)
            if image_event is not None:
                if text_output:
                    # 兼容“图片 + 文本”场景：文本先挂到 metadata，后续如果需要可在上层渲染。
                    image_event.metadata = {
                        **(image_event.metadata or {}),
                        "text": text_output,
                    }
                return image_event

            return text_output

        except Exception as e:
            logger.error(f"[mcp] call_tool 失败: {tool_id} — {e}")
            return f"[MCP 错误] {type(e).__name__}: {e}"

    return Tool(
        name=tool_id,
        description=description,
        parameters=parameters,
        func=_execute,
    )


async def build_mcp_tools_for_servers(
    manager: McpManager,
    server_names: set[str],
) -> list[Tool]:
    """为指定服务器构建 ftre Tool 实例列表。

    只从 server_names 指定的服务器发现工具，不影响连接池中其他服务器。
    返回的 Tool 列表未注册到任何 registry，由调用方决定注册目标。

    Args:
        manager: McpManager 实例
        server_names: 需要构建工具的服务器名集合
    """
    mcp_tools = await manager.list_tools_for_servers(server_names)
    if not mcp_tools:
        return []

    tools: list[Tool] = []
    for server_name, mcp_tool in mcp_tools:
        try:
            tool = create_mcp_tool(server_name, mcp_tool, manager)
            tools.append(tool)
            logger.info(f"[mcp] 构建工具: {tool.name} (来自 {server_name})")
        except Exception as e:
            logger.warning(f"[mcp] 工具转换失败: {server_name}/{mcp_tool.name} — {e}")

    return tools


async def build_mcp_tools(manager: McpManager) -> list[Tool]:
    """从所有已连接的 MCP 服务器发现并转换工具

    Returns:
        ftre Tool 实例列表，可直接注册到 ToolRegistry
    """
    all_servers = set(manager.get_connected_servers())
    return await build_mcp_tools_for_servers(manager, all_servers)
