"""ftre-agent 的唯一 Provider Plugin。

本 Plugin 只创建并发布 AgentService。AgentLoop 属于
``ftre-agent-runtime``，由另一个 Provider 注册 Runtime Factory；两个职责不能
再次合并到同一个 Plugin。
"""

from __future__ import annotations

from cordis import Context

from .service import AgentService

inject = ()
provide = ("agents",)


def apply(ctx: Context, config=None) -> None:
    """提供唯一 ``agents`` Service，并绑定可逆生命周期。"""
    service = ctx.get("agents", strict=False)
    if service is not None:
        return

    service = AgentService()
    service.start()
    ctx.provide("agents", service)
    ctx.effect(lambda: service.close, label="agent-service:close")


__all__ = ["apply", "inject", "provide"]
