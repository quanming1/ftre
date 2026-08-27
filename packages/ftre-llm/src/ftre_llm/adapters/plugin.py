"""OpenAI 协议适配器 Provider Plugin。

``LlmService`` 只拥有路由注册表和调用生命周期，不直接 import 任何具体协议
实现。本 Plugin 才是 OpenAI Completions/Responses 的 Owner：它通过 Inject
拿到公开的 ``llm`` Service，注册两个可逆路由，并在卸载时撤销注册。
"""

from __future__ import annotations

from cordis import Context

from .openai_completions import OpenAICompletionsAdapter
from .openai_responses import OpenAIResponsesAdapter

# 适配器只依赖 LLM Service；它既不创建 Service，也不访问 Host Config/Agent。
inject = ("llm",)
provide = ()


def apply(ctx: Context, config=None):
    """注册 OpenAI 两种 wire 协议，并把每个句柄绑定到当前 Fiber。"""

    del config
    registrations = (
        ctx.llm.register_adapter("completions", OpenAICompletionsAdapter),
        ctx.llm.register_adapter("responses", OpenAIResponsesAdapter),
    )
    for provider, registration in zip(("completions", "responses"), registrations, strict=True):
        # ``register_adapter`` 返回可逆句柄；Provider Plugin 退出后路由必须消失，
        # 否则 LlmService 会保留一个指向已卸载代码的工厂。
        ctx.effect(lambda registration=registration: registration.dispose, label=f"llm:{provider}")


__all__ = ["apply", "inject", "provide"]
