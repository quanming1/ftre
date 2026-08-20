from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ftre.services.filesystem.policy import PathPolicy


class WorkspaceService:
    key = "workspaces"

    def __init__(self, sessions: Any | None = None, default: str = "") -> None:
        self.sessions = sessions
        self.default = default

    async def get(self, session_id: str) -> str:
        if self.sessions is None:
            return self.default or os.getcwd()
        session = await self.sessions.get_session(session_id)
        value = getattr(session, "workspace", None) if session is not None else None
        if isinstance(session, dict):
            value = session.get("workspace")
        return str(value or self.default or os.getcwd())

    async def set(self, session_id: str, absolute_path: str) -> dict[str, str]:
        target = Path(absolute_path).expanduser().resolve()
        if not target.is_dir():
            raise NotADirectoryError(str(target))
        before = await self.get(session_id)
        if self.sessions is not None:
            await self.sessions.update_session(session_id, workspace=str(target))
        return {"before": before, "after": str(target)}

    async def policy(self, session_id: str, allowed_dirs: tuple[str, ...] = ()) -> PathPolicy:
        root = Path(await self.get(session_id)).expanduser().resolve()
        return PathPolicy(root=root, allowed_dirs=tuple(Path(item) for item in allowed_dirs))

    async def ensure_extension_layout(self, session_id: str) -> dict[str, str]:
        root = Path(await self.get(session_id)).expanduser().resolve()
        skills = root / ".ftre" / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        return {"workspace": str(root), "skills": str(skills), "mcp": str(root / ".ftre" / "mcp.json")}

