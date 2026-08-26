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
    async def dispatch(name, payload):
        return await ctx.hook_runtime.dispatch(spec_for(name), payload, context=ctx)

    service = LlmService(
        resolve_credentials=ctx.config.resolve_llm,
        hook_dispatch=dispatch,
    )
    ctx.provide("llm", service)
    ctx.effect(lambda: service.close, label="llm:close")


__all__ = ["apply", "inject", "provide"]
