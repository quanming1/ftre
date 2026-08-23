"""Agent Service 的 Provider Plugin。

本 Plugin 只发布 ``agents`` 这个稳定 key，不创建数据面执行器。Driver 会在独立
Provider 中完成组合后再绑定，这样 Agent Service 的启动顺序和执行实现可以独立
演进。
"""

from __future__ import annotations

from cordis import Context

from .service import AgentService

inject = ("agent_profiles",)
provide = ("agents",)


def apply(ctx: Context, config=None):
    """发布 Agent 门面；若 Composition 已注入实例则保持其所有权不变。"""
    if ctx.get("agents", strict=False) is not None:
        return
    ctx.provide("agents", AgentService(ctx.agent_profiles))
