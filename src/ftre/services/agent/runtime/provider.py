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

from .engine import AgentLoop


def build_runtime(
    ctx: Context,
    agent_service,
) -> AgentLoop:
    """从已注入的公开 Service 组装一个私有 AgentLoop。"""
    return AgentLoop(
        message_bus=ctx.message_bus,
        sessions=ctx.sessions,
        tools=ctx.tools,
        workspaces=ctx.workspaces,
        profiles=ctx.agent_profiles,
        config_service=ctx.config,
        agent_service=agent_service,
        attachments=ctx.get("attachments", strict=False),
        system_prompt=ctx.system_prompt,
        hook_runtime=ctx.hook_runtime,
        traces=ctx.get("traces", strict=False),
        session_events=ctx.session_events,
        llm_service=ctx.llm,
    )


__all__ = ["build_runtime"]
