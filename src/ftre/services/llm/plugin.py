"""LLM Service 的 Host Provider Plugin。

这里仅创建并公开 ``llm`` Service。具体协议适配器由 ``ftre-llm`` 自己的
Provider Plugin 注册，避免 Host Service 反向拥有 concrete adapter。
"""

from __future__ import annotations

from cordis import Context
from ftre_llm import LlmService

from .hooks import spec_for

inject = ("config", "hook_runtime")
provide = ("llm",)


async def apply(ctx: Context, config=None):
    """创建 LlmService 并绑定可逆关闭。"""

    service = ctx.get("llm", strict=False)
    if service is not None:
        return
    # Cordis 注入属性只保证在当前 apply Fiber 的依赖解析阶段可读；把已经解析的
    # HookRuntime 捕获为闭包值，避免后续异步 adapters-updated 回调再次把 Plugin
    # Context 当作 Service Locator 使用。
    hook_runtime = ctx.hook_runtime
    plugin_context = ctx

    async def dispatch(name, payload):
        return await hook_runtime.dispatch(spec_for(name), payload, context=plugin_context)

    service = LlmService(
        resolve_credentials=ctx.config.resolve_llm,
        hook_dispatch=dispatch,
    )
    ctx.provide("llm", service)
    ctx.effect(lambda: service.close, label="llm:close")


__all__ = ["apply", "inject", "provide"]
