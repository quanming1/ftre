"""Tool Service：全局工具、Agent scoped 工具和限制策略的唯一注册表。

全局 ``ToolRegistry`` 只保存可执行工具；Service 额外保存 owner/source/scope，
并在每个 Agent 请求时生成隔离 view。这样一个 Agent 的 allow/deny 或 scoped shadow
不会修改其他 Agent 的工具视图，Plugin 卸载也能通过 disposer 撤销贡献。

F34 起内置工具由 core-tools Plugin 作为普通贡献注册，本模块不再硬编码构造；
底层 registry 是 Owner 私有实现，外部只能通过 snapshot/get/schemas/execute
和 prepare_view 消费。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from ftre_agent_core.tool import ToolRegistry

from .filtering import filter_tools
from .scope import ToolRestriction
from .types import ToolContribution


class ToolService:
    """拥有工具注册、Agent 限制和可逆贡献。"""

    key = "tools"

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        # 底层可执行注册表是 Owner 私有实现：全局 scope 工具会同步登记到
        # 这里供 execute 使用，但外部不得直接访问，避免绕过作用域与贡献生命周期。
        self._registry = registry or ToolRegistry()
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
            self._registry.register(tool)
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
            # 只看同名 global 贡献是否仍存在：scoped shadow 不持有 _registry
            # 条目，不能因为它同名存在而跳过 global 的注销，否则卸载后残留
            # 一个仍可被 execute() 执行的旧 global 工具（生命周期泄漏）。
            if scope == "global" and not any(
                item.name == name and item.scope == "global" for item in self._items
            ):
                self._registry.unregister(name)
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

    def get(self, name: str, agent_id: str | None = None) -> ToolContribution | None:
        """作用域感知的单工具贡献查询；不可见或不存在时返回 None。"""
        for item in self._visible(agent_id):
            if item.name == name:
                return item
        return None

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
        """准备一个隔离 Core ToolRegistry，完成可选 Provider 和权限过滤。

        顺序：先运行可逆 view preparer（如 MCP 的 agent-scoped 准备），
        再合并该 Agent 可见的全部贡献（内置工具也是普通贡献），
        最后应用 profile 的 tools.allow/deny（不豁免任何来源）。
        """
        for _owner, preparer in tuple(self._view_preparers):
            result = preparer(agent_id, session_id, profile_config, llm_config)
            if inspect.isawaitable(result):
                await result

        view = ToolRegistry()
        for item in self._visible(agent_id):
            view.register(item.tool)
        tools_config = _profile_value(profile_config, "tools_config")
        if tools_config:
            filter_tools(view, tools_config)
        return view

    def execute(
        self,
        name: str,
        execution_context: dict | None = None,
        arguments=None,
        *,
        agent_id: str | None = None,
    ) -> Any:
        """Execute through the established tool contract.

        ``get/schemas/execute`` 共享同一作用域投影：传入 ``agent_id`` 时执行
        该 Agent 投影解析出的工具——scoped shadow 覆盖同名 global，仅存在于
        agent scope 的工具也可执行；不可见抛 ``KeyError``。不传 ``agent_id``
        保持全局 registry 语义。
        """
        if agent_id is None:
            return self._registry.execute(name, execution_context, **(arguments or {}))
        contribution = self.get(name, agent_id)
        if contribution is None:
            raise KeyError(f"tool {name!r} is not visible to agent {agent_id!r}")
        if contribution.scope == "global":
            # global 贡献与 _registry 同步登记，直接走全局执行路径。
            return self._registry.execute(name, execution_context, **(arguments or {}))
        # scoped 工具从不进入 _registry；经一次性单工具 registry 执行，
        # 保证注入解析与执行契约与 global 路径完全一致。
        scratch = ToolRegistry()
        scratch.register(contribution.tool)
        return scratch.execute(name, execution_context, **(arguments or {}))

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
