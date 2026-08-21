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
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ftre.services.session import SessionService

logger = logging.getLogger(__name__)

# 会话工作区扩展目录：<cwd>/.ftre，其下可放 skills/ 与 mcp.json
WORKSPACE_EXT_DIR = ".ftre"
WORKSPACE_MCP_FILE = "mcp.json"


def ensure_workspace_ext_dir(cwd: str) -> None:
    """确保 <cwd>/.ftre 扩展骨架存在（skills/ 目录 + 空 mcp.json），并把它加入 .gitignore。

    在处理 user 消息、cwd 确定后调用一次：给当前工作区默认建好扩展目录骨架，
    方便用户直接往里放工作区级 skill / mcp。cwd 为空或不是已存在目录时跳过
    （不对不存在的路径乱建）；已存在的文件不覆盖；失败只记日志，不影响主流程。
    .ftre 是运行时产物，默认写入工作区 .gitignore，避免被误提交进用户仓库。
    """
    if not cwd:
        return
    base = Path(cwd)
    if not base.is_dir():
        return
    ext_dir = base / WORKSPACE_EXT_DIR
    try:
        (ext_dir / "skills").mkdir(parents=True, exist_ok=True)
        mcp_file = ext_dir / WORKSPACE_MCP_FILE
        if not mcp_file.exists():
            mcp_file.write_text(
                json.dumps({"mcp": {}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except OSError as e:
        logger.warning("[workspace] 创建 %s/%s 失败: %s", cwd, WORKSPACE_EXT_DIR, e)
        return
    _ensure_gitignore_entry(base, f"{WORKSPACE_EXT_DIR}/")


def _ensure_gitignore_entry(base: Path, entry: str) -> None:
    """把 entry 追加到 <base>/.gitignore（幂等：已包含该行则跳过）。

    .gitignore 不存在则创建；末尾无换行时先补换行再追加。失败只记日志。
    """
    gitignore = base / ".gitignore"
    try:
        if gitignore.exists():
            content = gitignore.read_text(encoding="utf-8")
            lines = {ln.strip() for ln in content.splitlines()}
            if entry in lines:
                return
            prefix = "" if content == "" or content.endswith("\n") else "\n"
            with gitignore.open("a", encoding="utf-8") as f:
                f.write(f"{prefix}{entry}\n")
        else:
            gitignore.write_text(f"{entry}\n", encoding="utf-8")
    except (OSError, UnicodeError) as e:
        logger.warning("[workspace] 更新 %s/.gitignore 失败: %s", base, e)


@dataclass
class WorkspaceAccessor:
    """读写当前 session 工作区的同步外观（在工具线程里使用）。"""

    session_id: str
    session_manager: SessionService
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
