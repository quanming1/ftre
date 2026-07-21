"""
工具内部用的工作区访问器（runtime_context 注入的 'workspace' 就是它）。

设计动机：
- 之前 workspace 通过 `runtime_context['workspace'] = {'cwd': ...}` 注入，
  bash cd / set_workspace 直接改 dict 实现"会话级 cwd"。
- 把 workspace 升格为 sessions 表的一等字段后，dict 就成了多余的中间状态：
  改 dict 不落库会丢、刷新前端读不到。这里直接对 DB 做同步读写。

API：
- get(): 返回当前 session 的 workspace 绝对路径；DB 中为空 / 路径不存在
  时回退到 fallback_cwd（agent_loop 传入，通常是 config 默认 / 进程 cwd）。
- set(path): 写入 DB，返回旧值。

调用约束：
- 在同步工具线程里使用，借助 run_coroutine_threadsafe 把 async 调用抛回主 loop。
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ftre.session import SessionManager


@dataclass
class WorkspaceAccessor:
    """读写当前 session 工作区的同步外观（在工具线程里使用）。"""

    session_id: str
    session_manager: "SessionManager"
    event_loop: asyncio.AbstractEventLoop
    fallback_cwd: str

    def get(self) -> str:
        """
        返回当前 session 的 cwd。
        DB 中 workspace 非空且路径存在 → 用它；否则回退 fallback_cwd。
        """
        ws = self._read_db_workspace()
        if ws and os.path.isdir(ws):
            return ws
        return self.fallback_cwd

    def set(self, new_path: str) -> str:
        """写入 DB（不做存在性校验，调用方责任）。返回写入前的旧值。"""
        old = self.get()
        coro = self.session_manager.update_session(
            self.session_id, workspace=new_path
        )
        asyncio.run_coroutine_threadsafe(coro, self.event_loop).result()
        return old

    def _read_db_workspace(self) -> str:
        coro = self.session_manager.get_session(self.session_id)
        session = asyncio.run_coroutine_threadsafe(coro, self.event_loop).result()
        if not session:
            return ""
        return session.get("workspace") or ""
