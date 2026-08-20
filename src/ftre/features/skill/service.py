from __future__ import annotations

from pathlib import Path
from typing import Any

from .types import SkillRecord


class SkillService:
    key = "skills"

    def __init__(self, roots: dict[str, Path] | None = None) -> None:
        self.roots = roots or {"global": Path.home() / ".ftre" / "skills"}
        self._runtime: list[SkillRecord] = []
        self._loaded: dict[tuple[str, str], str] = {}

    def register(self, skill: SkillRecord, owner: str, scope: str = "global"):
        record = SkillRecord(skill.name, skill.content, owner, skill.source, skill.priority, scope)
        self._runtime.append(record)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._runtime.remove(record)
            except ValueError:
                return False
            return True

        return dispose

    def list(self, agent_id: str = "default", workspace: str | None = None) -> list[dict[str, Any]]:
        records = self._resolve(agent_id, workspace)
        return [{"name": item.name, "owner": item.owner, "source": item.source, "scope": item.scope, "priority": item.priority} for item in records]

    def get(self, name: str, agent_id: str = "default", workspace: str | None = None) -> SkillRecord | None:
        return next((item for item in self._resolve(agent_id, workspace) if item.name == name), None)

    def sources(self, name: str, agent_id: str = "default", workspace: str | None = None) -> dict[str, Any]:
        candidates = [item for item in self._all(agent_id, workspace) if item.name == name]
        ordered = sorted(candidates, key=lambda item: (item.priority, item.owner))
        return {"candidates": [item.__dict__ for item in ordered], "winner": ordered[0].owner if ordered else None, "shadowed": [item.owner for item in ordered[1:]]}

    def mark_loaded(self, session_id: str, name: str, source: str) -> None:
        self._loaded[(session_id, name)] = source

    def _all(self, agent_id: str, workspace: str | None) -> list[SkillRecord]:
        result = list(self._runtime)
        roots = []
        if workspace:
            roots.append((Path(workspace) / ".ftre" / "skills", 10, f"workspace:{workspace}"))
        roots.append((self.roots.get("agent", Path.home() / ".ftre" / "agents" / agent_id / "skills"), 20, f"agent:{agent_id}"))
        roots.append((self.roots.get("global", Path.home() / ".ftre" / "skills"), 30, "global"))
        for root, priority, scope in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.md")):
                name = path.stem if path.name.lower() != "skill.md" else path.parent.name
                try:
                    result.append(SkillRecord(name, path.read_text(encoding="utf-8", errors="replace"), "filesystem", str(root), priority, scope))
                except OSError:
                    continue
        return result

    def _resolve(self, agent_id: str, workspace: str | None) -> list[SkillRecord]:
        result: dict[str, SkillRecord] = {}
        for item in sorted(self._all(agent_id, workspace), key=lambda value: (value.priority, value.owner, value.name)):
            result.setdefault(item.name, item)
        return list(result.values())

