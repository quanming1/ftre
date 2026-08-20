from __future__ import annotations

from copy import copy
from typing import Any

from ftre_agent_core.tool import ToolRegistry

from .scope import ToolRestriction
from .types import ToolContribution


class ToolService:
    key = "tools"

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()
        self._items: list[ToolContribution] = []
        self._restrictions: list[ToolRestriction] = []

    def register(self, tool: Any, owner: str, scope: str = "global", source: str = "builtin"):
        name = str(tool.name)
        if any(item.name == name and item.scope == scope for item in self._items):
            raise ValueError(f"tool {name!r} already registered in {scope}")
        self.registry.register(tool)
        contribution = ToolContribution(name, owner, source, scope, tool)
        self._items.append(contribution)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._items.remove(contribution)
            except ValueError:
                return False
            if not any(item.name == name for item in self._items):
                self.registry.unregister(name)
            return True

        return dispose

    def restrict(self, agent_id: str, owner: str, allow=None, deny=None):
        restriction = ToolRestriction(agent_id, owner, frozenset(allow or ()), frozenset(deny or ()))
        self._restrictions.append(restriction)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._restrictions.remove(restriction)
            except ValueError:
                return False
            return True

        return dispose

    def snapshot(self, agent_id: str | None = None) -> tuple[ToolContribution, ...]:
        return tuple(item for item in self._visible(agent_id))

    def schemas(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        result = []
        for item in self._visible(agent_id):
            schema = item.tool.to_openai_dict() if hasattr(item.tool, "to_openai_dict") else {"name": item.name}
            result.append({**schema, "owner": item.owner, "source": item.source, "scope": item.scope})
        return result

    def build_view(self, agent_id: str, session_id: str | None = None) -> ToolRegistry:
        view = ToolRegistry()
        for item in self._visible(agent_id):
            view.register(item.tool)
        return view

    def execute(self, name: str, execution_context: dict | None = None, arguments=None) -> Any:
        return self.registry.execute(name, execution_context, **(arguments or {}))

    def _visible(self, agent_id: str | None):
        items = [item for item in self._items if item.scope == "global" or item.scope == f"agent:{agent_id}"]
        if agent_id is None:
            return items
        for restriction in reversed(self._restrictions):
            if restriction.agent_id != agent_id:
                continue
            if restriction.allow:
                items = [item for item in items if item.name in restriction.allow]
            items = [item for item in items if item.name not in restriction.deny]
        return items

