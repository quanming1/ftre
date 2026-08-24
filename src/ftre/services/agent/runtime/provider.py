"""Private Agent runtime construction.

Only ``services.agent.plugin`` calls this function. Keeping construction here
keeps the Agent Provider readable without creating a second public Service or
another lifecycle owner.
"""
# 私有 Agent Runtime 构造：唯一调用方是 services.agent.plugin（Agent Provider Plugin）。
# 把"从 ctx 注入的公开 Service 图组装成 AgentLoop"这一步独立成函数，
# 让 Provider 保持可读，同时不产生第二个公开 Service / 第二个生命周期 Owner。
# 按 PRD-F14 §5.2：AgentLoop 只是 AgentService 的私有 Runtime，业务方不得 import。

from __future__ import annotations

from cordis import Context

from ftre.kernel.plugins.manager import PluginManager

from .engine import AgentLoop


def build_runtime(
    ctx: Context,
    plugin_manager: PluginManager,
    agent_service,
) -> AgentLoop:
    """Construct one private Loop from the Provider's injected Service graph."""
    # 从 Composition 注入的 ctx 中取出 AgentLoop 所需的全部公开 Service 句柄，
    # 一次性构造私有 Loop。所有依赖都来自 ctx（Inject），不做 Service Locator。
    tools = ctx.tools
    kwargs = {
        # 消息总线：AgentLoop 从这里收 inbound / 发 outbound
        "bus": ctx.message_bus.bus,
        # Session 持久化与身份（写入正式消息历史）
        "session_manager": ctx.sessions,
        # 通道管理器：出站消息路由到具体 Channel
        "channel_manager": ctx.channels.manager,
        # 事件总线：HookRuntime / 事件广播共用同一个 Cordis Context
        "event_hub": ctx,
        # 工具执行视图（ToolRegistry 底层注册表 + ToolService 门面）
        "tool_registry": tools.registry,
        "tool_service": tools,
        # 可选能力：MCP 工具视图（未安装时为 None）
        "mcp_service": ctx.get("mcp", strict=False),
        # Plugin 生命周期：AgentLoop 内部如需查询/协作 Plugin 状态
        "plugin_manager": plugin_manager,
        # Agent 配置加载（~/.ftre/agents/<id>/）
        "agent_manager": ctx.agent_profiles.manager,
        # 唯一公开 Agent Service：Loop 把执行结果交回给它
        "agent_service": agent_service,
        # 附件服务（工具产生图片等附件时落盘）
        "attachments": ctx.get("attachments", strict=False),
        # Agent 注册表（active Turn 追踪）
        "agent_registry": agent_service.registry,
        # 可选能力：Trace 导出（未安装时为 None）
        "traces": ctx.get("traces", strict=False),
        # System Prompt 组装（结构化 section 合并）
        "system_prompt": ctx.system_prompt,
        # 语义 Hook 运行时（Agent/Tool/LLM 时机分发）
        "hook_runtime": ctx.hook_runtime,
        # Session 事件统一出口（由 SessionEventService 广播，无第二 Owner）
        "session_events": ctx.session_events,
    }
    return AgentLoop(**kwargs)


__all__ = ["build_runtime"]
