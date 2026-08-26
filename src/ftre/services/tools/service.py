"""Tool Service：全局工具、Agent scoped 工具和限制策略的唯一注册表。

全局 ``ToolRegistry`` 只保存可执行工具；Service 额外保存 owner/source/scope，
并在每个 Agent 请求时生成隔离 view。这样一个 Agent 的 allow/deny 或 scoped shadow
不会修改其他 Agent 的工具视图，Plugin 卸载也能通过 disposer 撤销贡献。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from ftre_agent_core.tool import ToolRegistry

from .scope import ToolRestriction
from .types import ToolContribution


class ToolService:
    """拥有工具注册、Agent 限制和可逆贡献。"""
    key = "tools"

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()
        self._items: list[ToolContribution] = []
        self._restrictions: list[ToolRestriction] = []
        # 可选工具 Provider（例如 MCP）在建视图前贡献 agent-scoped 工具；
        # disposer 由注册它的 Plugin 持有，卸载后不会继续影响后续 Turn。
        self._view_preparers: list[tuple[str, Callable[..., Any]]] = []

    def register(self, tool: Any, owner: str, scope: str = "global", source: str = "builtin"):
        """Register a tool in a scope and return the Fiber cleanup callback."""
        name = str(tool.name)
        if any(item.name == name and item.scope == scope for item in self._items):
            raise ValueError(f"tool {name!r} already registered in {scope}")
        if scope == "global":
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
            if scope == "global" and not any(item.name == name for item in self._items):
                self.registry.unregister(name)
            return True

        return dispose

    def restrict(self, agent_id: str, owner: str, allow=None, deny=None):
        """Add a reversible allow/deny policy for one Agent's tool view."""
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

    def register_view_preparer(
        self, preparer: Callable[..., Any], *, owner: str
    ) -> Callable[[], bool]:
        """注册一个在 Agent Tool View 创建前运行的可逆准备器。"""
        if not callable(preparer):
            raise TypeError("preparer must be callable")
        entry = (owner, preparer)
        self._view_preparers.append(entry)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._view_preparers.remove(entry)
            except ValueError:
                return False
            return True

        return dispose

    def snapshot(self, agent_id: str | None = None) -> tuple[ToolContribution, ...]:
        """Return visible contributions for an Agent or the global view."""
        return tuple(item for item in self._visible(agent_id))

    def schemas(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """Return OpenAI-compatible schemas enriched with ownership metadata."""
        result = []
        for item in self._visible(agent_id):
            schema = item.tool.to_openai_dict() if hasattr(item.tool, "to_openai_dict") else {"name": item.name}
            result.append({**schema, "owner": item.owner, "source": item.source, "scope": item.scope})
        return result

    async def prepare_view(
        self,
        agent_id: str,
        session_id: str,
        profile_config: Any | None = None,
        *,
        llm_config: Any | None = None,
    ) -> ToolRegistry:
        """准备一个隔离 Core ToolRegistry，完成可选 Provider 和权限过滤。"""
        mcp_config = _profile_value(profile_config, "mcp_config")
        for _owner, preparer in tuple(self._view_preparers):
            result = preparer(agent_id, session_id, mcp_config)
            if inspect.isawaitable(result):
                await result

        view = ToolRegistry()
        from .builtin import (
            create_bash_tool,
            create_edit_tool,
            create_read_tool,
            create_set_workspace_tool,
            create_write_tool,
            filter_tools,
        )

        view.register(create_bash_tool())
        view.register(create_read_tool(vision=getattr(llm_config, "vision", False)))
        view.register(create_write_tool())
        view.register(create_edit_tool())
        view.register(create_set_workspace_tool())
        for item in self._visible(agent_id):
            view.register(item.tool)
        tools_config = _profile_value(profile_config, "tools_config")
        if tools_config:
            filter_tools(view, tools_config)
        return view

    def execute(self, name: str, execution_context: dict | None = None, arguments=None) -> Any:
        """Execute only through the global registry's established tool contract."""
        return self.registry.execute(name, execution_context, **(arguments or {}))

    def _visible(self, agent_id: str | None):
        """Resolve scoped shadowing first, then apply restrictions newest-first."""
        candidates = [item for item in self._items if item.scope == "global" or item.scope == f"agent:{agent_id}"]
        by_name: dict[str, ToolContribution] = {}
        for item in candidates:
            # A scoped contribution shadows the global one with the same name;
            # stable registration order remains the tie-breaker.
            by_name[item.name] = item
        items = list(by_name.values())
        if agent_id is None:
            return items
        for restriction in reversed(self._restrictions):
            if restriction.agent_id != agent_id:
                continue
            if restriction.allow:
                items = [item for item in items if item.name in restriction.allow]
            items = [item for item in items if item.name not in restriction.deny]
        return items


def _profile_value(profile_config: Any, field: str) -> Any:
    """从 AgentProfile 或 mapping 读取配置，不依赖 Manager 私有类型。"""
    if profile_config is None:
        return None
    if isinstance(profile_config, Mapping):
        return profile_config.get(field)
    return getattr(profile_config, field, None)
