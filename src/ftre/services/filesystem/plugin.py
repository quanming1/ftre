"""本地文件系统 Service 的 Provider Plugin。

只发布一个 ``filesystem`` Owner，避免不同工具各自实现路径穿越检查，导致安全
边界不一致。
"""

from __future__ import annotations

from cordis import Context

from .local import LocalFilesystemService

provide = ("filesystem",)
inject = ()


def apply(ctx: Context, config=None):
    """发布共享文件系统门面；所有路径校验从同一 Owner 进入。"""
    if ctx.get("filesystem", strict=False) is not None:
        return
    ctx.provide("filesystem", LocalFilesystemService())
