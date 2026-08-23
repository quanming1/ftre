"""Agent Service 的唯一 Provider Plugin。

它同时创建公开的 ``agents`` Service 和其私有 Runtime；Runtime 没有第二个
Context key，避免 AgentService 与 AgentLoop 形成两个可见 Owner。
"""

from __future__ import annotations

from cordis import Context

from .runtime.driver import AgentLoopDriver
from .runtime.provider import build_runtime
from .service import AgentService

inject = (
    "agent_profiles",
    "sessions",
    "message_bus",
    "channels",
    "tools",
    "system_prompt",
    "hook_runtime",
    "plugin_manager",
)
provide = ("agents",)


def apply(ctx: Context, config=None):
    """创建 AgentService、私有 Runtime 并把两者绑定到同一个 Fiber。"""
    if ctx.get("agents", strict=False) is not None:
        return

    service = AgentService(ctx.agent_profiles)
    # 先 provide 公开 Service，再把同一实例显式传给私有 Runtime；不通过
    # Context 反查自己，也不向外发布第二个 Runtime Service 句柄。
    ctx.provide("agents", service)
    loop = build_runtime(ctx, ctx.plugin_manager, service)
    driver = AgentLoopDriver(loop)
    service.attach_driver(driver, ctx.agent_profiles)
    loop.start()

    async def close() -> None:
        await loop.stop()
        service.detach_driver()

    ctx.effect(lambda: close, label="agent:runtime")
