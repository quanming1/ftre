"""Workspace Service：Session 工作区选择和文件系统策略构造。

工作区属于 Session，而不是进程全局配置；Service 从 SessionService 读取/写回当前
目录，并把它转换成 Tools 可消费的 ``PathPolicy``。它不直接执行文件读写，避免
工作区状态和文件 IO 的 Owner 混在一起。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ftre.services.filesystem.policy import PathPolicy

from .accessor import WorkspaceAccessor

logger = logging.getLogger(__name__)

WORKSPACE_EXT_DIR = ".ftre"


def ensure_workspace_ext_dir(cwd: str) -> None:
    """创建工作区扩展骨架；失败只记录，不阻断 Agent Turn。"""
    if not cwd:
        return
    base = Path(cwd)
    if not base.is_dir():
        return
    ext_dir = base / WORKSPACE_EXT_DIR
    try:
        (ext_dir / "skills").mkdir(parents=True, exist_ok=True)
        mcp_file = ext_dir / "mcp.json"
        if not mcp_file.exists():
            mcp_file.write_text(
                json.dumps({"mcp": {}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        gitignore = base / ".gitignore"
        # .gitignore 可能由旧项目以 GBK/本地编码保存。这里仅追加固定 ASCII
        # 规则并保留原始字节，避免创建扩展目录时破坏用户已有注释和换行风格。
        current = gitignore.read_bytes() if gitignore.exists() else b""
        entry = f"{WORKSPACE_EXT_DIR}/".encode("ascii")
        if not any(line.strip() == entry for line in current.splitlines()):
            separator = b"" if current.endswith((b"\n", b"\r")) else b"\r\n" if current else b""
            gitignore.write_bytes(current + separator + entry + b"\r\n")
    except OSError as exc:
        logger.warning("[workspace] 创建 %s 扩展目录失败: %s", cwd, exc)


class WorkspaceService:
    """把 Session 工作区状态转换为安全的文件系统边界。"""
    key = "workspaces"

    def __init__(self, sessions: Any | None = None, default: str = "") -> None:
        self.sessions = sessions
        self.default = default

    async def get(self, session_id: str) -> str:
        """Read the persisted workspace, falling back to the process directory."""
        if self.sessions is None:
            return self.default or os.getcwd()
        session = await self.sessions.get_session(session_id)
        value = getattr(session, "workspace", None) if session is not None else None
        if isinstance(session, dict):
            value = session.get("workspace")
        return str(value or self.default or os.getcwd())

    async def set(self, session_id: str, absolute_path: str) -> dict[str, str]:
        """Validate and persist a directory change, returning before/after values."""
        target = Path(absolute_path).expanduser().resolve()
        if not target.is_dir():
            raise NotADirectoryError(str(target))
        before = await self.get(session_id)
        if self.sessions is not None:
            await self.sessions.update_session(session_id, workspace=str(target))
        return {"before": before, "after": str(target)}

    async def policy(self, session_id: str, allowed_dirs: tuple[str, ...] = ()) -> PathPolicy:
        """Create the path policy used by file tools for the session workspace."""
        root = Path(await self.get(session_id)).expanduser().resolve()
        return PathPolicy(root=root, allowed_dirs=tuple(Path(item) for item in allowed_dirs))

    async def ensure_extension_layout(self, session_id: str) -> dict[str, str]:
        """Create workspace extension directories and return their stable paths."""
        root = Path(await self.get(session_id)).expanduser().resolve()
        ensure_workspace_ext_dir(str(root))
        skills = root / ".ftre" / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        return {"workspace": str(root), "skills": str(skills), "mcp": str(root / ".ftre" / "mcp.json")}

    def create_accessor(
        self,
        session_id: str,
        event_loop,
        *,
        fallback_cwd: str,
    ) -> WorkspaceAccessor:
        """为同步 Core Tool 创建工作区访问器，具体实现归 Workspace Owner。"""
        return WorkspaceAccessor(
            session_id=session_id,
            session_manager=self.sessions,
            event_loop=event_loop,
            fallback_cwd=fallback_cwd,
        )
