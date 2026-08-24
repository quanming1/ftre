"""Tool Service 的 Provider Plugin。

它只创建注册表，不在 import 时注册工具；内置工具和外部 Feature 通过公开 Service
贡献，生命周期由各自 Plugin 的 ``ctx.effect`` 管理。
"""

from __future__ import annotations

from cordis import Context

from .service import ToolService

inject = ()
provide = ("tools",)


def apply(ctx: Context, config=None):
    """发布供内置/外部 Feature 使用的 ToolService。"""
    if ctx.get("tools", strict=False) is not None:
        return
    ctx.provide("tools", ToolService())
