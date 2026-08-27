"""ftre-agent-runtime 的唯一 Provider Plugin。

它同时创建公开的 ``agents`` Service（来自 ftre-agent 契约包）和私有执行
Runtime，并把两者绑定到同一个 Fiber；Runtime 没有第二个 Context key，避免
AgentService 与 AgentLoop 形成两个可见 Owner。

Host Service 依赖全部通过 ``inject`` 声明后由 Cordis 注入；``attachments`` 与
``traces`` 是可选能力，由本 Provider 显式解析后传入，Runtime 代码自身不调用
``ctx.get()``（PRD-F33 §5.2）。
"""

from __future__ import annotations

from cordis import Context
from ftre_agent import AgentService

from .engine import AgentLoop

inject = (
    "config",
    "agent_profiles",
    "sessions",
    "message_bus",
    "tools",
    "workspaces",
    "system_prompt",
    "hook_runtime",
    "session_events",
    "llm",
)
provide = ("agents",)


def apply(ctx: Context, config=None):
    """创建 AgentService、私有 Runtime 并把两者绑定到同一个 Fiber。"""
    if ctx.get("agents", strict=False) is not None:
        return

    service = AgentService()
    # 先 provide 公开 Service，再把同一实例显式传给私有 Runtime；不通过
    # Context 反查自己，也不向外发布第二个 Runtime Service 句柄。
    ctx.provide("agents", service)
    loop = AgentLoop(
        message_bus=ctx.message_bus,
        sessions=ctx.sessions,
        tools=ctx.tools,
        workspaces=ctx.workspaces,
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
    service.attach_runtime(loop)
    loop.start()

    async def close() -> None:
        await loop.stop()
        service.detach_runtime()

    ctx.effect(lambda: close, label="agent:runtime")
