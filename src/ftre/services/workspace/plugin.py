"""Workspace Service 的 Provider Plugin。

它注入 ``sessions`` 作为状态 Owner；Workspace 自身只提供路径解析和策略，不会
复制一份 Session 数据，也不会绕过 SessionService 直接读 state.json。
"""

from __future__ import annotations

from cordis import Context

from .service import WorkspaceService

inject = ("sessions",)
provide = ("workspaces",)


def apply(ctx: Context, config=None):
    """发布通过 SessionService 解析工作区的门面。"""
    if ctx.get("workspaces", strict=False) is not None:
        return
    ctx.provide("workspaces", WorkspaceService(ctx.sessions))
