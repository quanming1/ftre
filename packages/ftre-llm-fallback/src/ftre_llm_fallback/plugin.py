"""把备用模型流包装器注册到 ``llm/stream`` 的 Cordis Plugin 入口。

``llm/stream`` 在 Runtime 每次真正调用主模型前触发。listener 先通过 ``next_()`` 获得下游
提供的主模型流，再返回一个惰性的包装流。只有 Runtime 随后开始 ``async for`` 消费时，
``stream_with_fallback`` 才实际执行，因此不会在 Hook dispatch 阶段提前调用 LLM。
"""

from __future__ import annotations

from cordis import Context

from ftre.services.llm.hooks import LLM_STREAM_SPEC

from .config import parse_config
from .stream import stream_with_fallback

# config 是 Host 的 ConfigService：只用来把 provider/model 名称解析成一次性 Adapter 参数。
# hook_runtime 管理 listener 的 Agent Scope、in-flight 调用和 Fiber 卸载清理。
# 本 Plugin 不 provide Service，因为“换备用模型”只是一个可选时机行为，不是共享状态能力。
inject = ("config", "hook_runtime", "llm")
provide = ()


def apply(ctx: Context, config=None):
    """冻结当前配置，并注册一个可逆的 Waterfall Hook listener。"""

    # 每个 Plugin Fiber 固定一份配置，避免一次请求消费流期间规则突然变化。
    snapshot = parse_config(config)

    async def on_stream(payload, next_):
        """取得下游主模型流，并在启用时给它套上最后一次 fallback 保护。"""

        # next_ 可能是其他 llm/stream Plugin，也可能最终调用 payload.invoke()。
        # 保留 Waterfall 顺序才能让多个流包装行为按注册次序组合。
        primary = await next_()
        if not snapshot.enabled:
            return primary
        return stream_with_fallback(payload, primary, ctx.config, snapshot, ctx.llm)

    # context=ctx 把 Receipt 绑定到当前 Fiber。unload/restart 后旧 listener 自动失效，
    # all_agent_scopes=True 表示所有 Agent Scope 都应用同一套 Host fallback 策略。
    ctx.hook_runtime.register(
        LLM_STREAM_SPEC,
        on_stream,
        owner="ftre-llm-fallback",
        context=ctx,
        all_agent_scopes=True,
    )


__all__ = ["apply", "inject", "provide"]
