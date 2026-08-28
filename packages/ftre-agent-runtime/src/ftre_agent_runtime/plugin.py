"""ftre-agent-runtime 的 Runtime Provider Plugin。

它只创建私有 AgentLoop，并把 Loop 注册为已经存在的 ``agents`` Service 的
Runtime Factory。公开 AgentService 由 ``ftre-agent.plugin`` 唯一提供。

Host Service 依赖全部通过 ``inject`` 声明后由 Cordis 注入；``attachments`` 与
``traces`` 是可选能力，由本 Provider 显式解析后传入，Runtime 代码自身不调用
``ctx.get()``（PRD-F35.1）。
"""

from __future__ import annotations

from cordis import Context

from .engine import AgentLoop
from .runtime_factory import AgentLoopFactory

inject = (
    "agents",
    "config",
    "agent_profiles",
    "sessions",
    "message_bus",
    "tools",
    "workspaces",
    "process",
    "system_prompt",
    "hook_runtime",
    "session_events",
    "llm",
)
provide = ()


def apply(ctx: Context, config=None):
    """创建私有 Runtime，并注册到 AgentService 的唯一 Factory 槽位。"""
    service = ctx.agents
    loop = AgentLoop(
        message_bus=ctx.message_bus,
        sessions=ctx.sessions,
        tools=ctx.tools,
        workspaces=ctx.workspaces,
        process_service=ctx.process,
        profiles=ctx.agent_profiles,
        config_service=ctx.config,
        agent_service=service,
        attachments=ctx.get("attachments", strict=False),
        system_prompt=ctx.system_prompt,
        hook_runtime=ctx.hook_runtime,
        traces=ctx.get("traces", strict=False),
        session_events=ctx.session_events,
        llm_service=ctx.llm,
    )
    factory = AgentLoopFactory(loop)
    registration = service.register_factory(factory)
    factory.start()

    async def close() -> None:
        try:
            await factory.stop()
        finally:
            service.unregister_factory(registration)

    ctx.effect(lambda: close, label="agent:runtime")
