"""同步工具使用的工作区访问器。

文件工具运行在同步调用边界，而 WorkspaceService 的公开 API 是异步的。
这个小型适配对象属于 Workspace Owner；Agent Runtime 只向 Service 请求它，
不再 import 工具目录里的私有实现。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ftre.services.session import SessionService


@dataclass
class WorkspaceAccessor:
    """在工具线程中同步读写当前 Session 的持久化工作区。"""

    session_id: str
    session_manager: SessionService
    event_loop: asyncio.AbstractEventLoop
    fallback_cwd: str

    def get(self) -> str:
        """返回有效工作区；数据库为空或目录不存在时回退默认目录。"""
        workspace = self._read_db_workspace()
        if workspace and os.path.isdir(workspace):
            return workspace
        return self.fallback_cwd

    def set(self, new_path: str) -> str:
        """同步等待 SessionService 持久化新目录，并返回旧目录。"""
        old = self.get()
        future = self.session_manager.update_session(
            self.session_id,
            workspace=new_path,
        )
        asyncio.run_coroutine_threadsafe(future, self.event_loop).result()
        return old

    def _read_db_workspace(self) -> str:
        future = self.session_manager.get_session(self.session_id)
        session = asyncio.run_coroutine_threadsafe(future, self.event_loop).result()
        if not session:
            return ""
        return session.get("workspace") or ""


__all__ = ["WorkspaceAccessor"]
