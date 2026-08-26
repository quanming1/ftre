"""Feature Plugin for MCP connection state and tool registration."""
# MCP Feature Plugin：创建连接管理器（McpManager）与状态服务（McpService），
# 以 mcp key 发布到 ctx；启动时若有全局 mcp 配置则立即连接注册。
# Agent 私有 MCP 由 McpService.prepare_agent() 在 turn 前按 profile 加载（见 service.py）。

from __future__ import annotations

from cordis import Context

from .connection import McpManager
from .service import McpService

inject = ("config", "tools", "attachments", "http")
provide = ("mcp",)


async def apply(ctx: Context, config=None):
    """Publish MCP state and own the transport manager's full lifecycle."""
    # 防御：已存在同 key（bootstrap 注入）时跳过，保证单实例
    if ctx.get("mcp", strict=False) is not None:
        return
    manager = McpManager(
        tool_service=ctx.tools,
        attachment_service=ctx.attachments,
    )
    service = McpService(manager, tool_service=ctx.tools)
    ctx.provide("mcp", service)
    async def prepare_view(agent_id, _session_id, profile_config, _llm_config):
        """让 Tools Owner 在建 scoped view 前准备 MCP 工具。

        preparer 契约（F34）是通用的 (agent_id, session_id, profile_config,
        llm_config)；mcp_config 字段由 MCP 自己从 profile 片段中读取。
        """
        mcp_config = (
            profile_config.get("mcp_config")
            if isinstance(profile_config, dict)
            else getattr(profile_config, "mcp_config", None)
        )
        await service.prepare_agent(agent_id, mcp_config)

    view_disposer = ctx.tools.register_view_preparer(
        prepare_view,
        owner="mcp",
    )
    ctx.effect(lambda: view_disposer, label="mcp:tool-view-preparer")
    # 全局配置：只从 config.json 的 mcp 段加载（Agent 私有配置走 prepare_agent）
    raw = ctx.config.snapshot().value.get("mcp", {})
    if isinstance(raw, dict) and raw:
        await service.start_and_register(raw)
    # 卸载时停止全部连接与 watcher（可逆）
    ctx.effect(lambda: service.stop, label="mcp:stop")
    from .router import build_router

    route_disposer = ctx.http.register_router(build_router(service), owner="mcp")
    ctx.effect(lambda: route_disposer, label="http:mcp")
