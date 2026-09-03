"""Workspace Service：Session 工作区选择和文件系统策略构造。

工作区属于 Session，而不是进程全局配置；Service 从 SessionService 读取/写回当前
目录，并把它转换成 Tools 可消费的 ``PathPolicy``。它不直接执行文件读写，避免
工作区状态和文件 IO 的 Owner 混在一起。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
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

    def mcp_source(self, workspace: str) -> dict[str, Any]:
        """Read one workspace's MCP layer without exposing raw file ownership.

        A missing file is an empty layer. Invalid JSON is intentionally reported
        as an empty runtime layer here; the MCP Feature reads diagnostics through
        ``mcp_source_error`` so a broken project file never blocks unrelated
        sessions or causes the Workspace Service to leak parser details.
        """
        path = self._mcp_path(workspace)
        if path is None or not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        raw = value.get("mcp", value)
        return dict(raw) if isinstance(raw, dict) else {}

    def mcp_source_error(self, workspace: str) -> str | None:
        """Return a user-facing parsing error for a project MCP source, if any."""
        path = self._mcp_path(workspace)
        if path is None or not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return f"无法读取项目 MCP 配置: {exc}"
        if not isinstance(value, dict):
            return "项目 MCP 配置根节点必须是对象"
        raw = value.get("mcp", value)
        if not isinstance(raw, dict):
            return "项目 MCP 配置的 mcp 字段必须是对象"
        return None

    def replace_mcp_source(self, workspace: str, entries: dict[str, Any]) -> dict[str, Any]:
        """Atomically replace only ``.ftre/mcp.json``'s MCP object.

        The caller supplies a complete source layer. This method owns path
        validation and atomic persistence so the MCP Plugin never writes a
        workspace file directly.
        """
        if not isinstance(entries, dict):
            raise TypeError("workspace MCP entries must be an object")
        path = self._mcp_path(workspace, require_directory=True)
        if path is None:
            raise NotADirectoryError(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, {"mcp": entries})
        return dict(entries)

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
        """Persist a workspace-owned JSON object using temp + replace."""
        fd, temporary = tempfile.mkstemp(prefix=".mcp.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _mcp_path(workspace: str, *, require_directory: bool = False) -> Path | None:
        """Resolve a workspace MCP path while rejecting non-directory inputs."""
        if not isinstance(workspace, str) or not workspace.strip():
            return None
        root = Path(workspace).expanduser().resolve()
        if require_directory and not root.is_dir():
            return None
        if not require_directory and not root.is_dir():
            return None
        return root / WORKSPACE_EXT_DIR / "mcp.json"

    def create_accessor(
        self,
        session_id: str,
        event_loop,
        *,
        fallback_cwd: str,
    ) -> WorkspaceAccessor:
        """为同步 Tool 创建工作区访问器，具体实现归 Workspace Owner。"""
        return WorkspaceAccessor(
            session_id=session_id,
            session_manager=self.sessions,
            event_loop=event_loop,
            fallback_cwd=fallback_cwd,
        )
