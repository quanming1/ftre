"""MCP Feature Service: three configuration layers, catalog and ToolView wiring.

McpService is the only owner that understands MCP configuration sources and
runtime connection state. ConfigService, AgentProfileService and
WorkspaceService retain ownership of their files; ToolService retains ownership
of contribution visibility and execution.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .adapter import MCP_TOOL_PREFIX, build_mcp_tools_for_servers
from .config import inspect_mcp_server
from .connection import McpManager

logger = logging.getLogger(__name__)

McpScope = Literal["global", "agent", "project"]
McpView = Literal["effective", "sources"]
McpStatus = Literal["configured", "connecting", "connected", "failed", "disabled", "invalid"]

_SCOPES: tuple[McpScope, ...] = ("global", "agent", "project")
_WINNER_ORDER: tuple[McpScope, ...] = ("project", "agent", "global")
_SAFE_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_MISSING = object()


@dataclass(frozen=True)
class McpCatalogItem:
    """A UI-safe MCP source or effective entry."""

    name: str
    scope: McpScope
    status: McpStatus
    config: dict[str, Any]
    effective: bool
    shadowed_by: McpScope | None = None
    error: str | None = None
    tools_count: int = 0

    def to_public_dict(self) -> dict[str, Any]:
        """Return the stable HTTP representation used by desktop clients."""
        return {
            "name": self.name,
            "scope": self.scope,
            "status": self.status,
            "effective": self.effective,
            "shadowed_by": self.shadowed_by,
            "error": self.error,
            "tools_count": self.tools_count,
            **copy.deepcopy(self.config),
        }


@dataclass
class _PreparedScope:
    """One Agent or Session layer's reversible ToolService contributions."""

    signature: str
    agent_id: str
    workspace: str | None = None
    manager_refs: dict[str, tuple[str, McpManager]] = field(default_factory=dict)
    tool_names: dict[str, tuple[str, ...]] = field(default_factory=dict)
    disposers: list[object] = field(default_factory=list)
    restrictions: list[object] = field(default_factory=list)


class McpService:
    """Resolve MCP layers, expose a catalog, and prepare isolated ToolViews.

    Source precedence is project > agent > global. Runtime visibility mirrors
    that order through ToolService scopes: global → agent → session.
    """

    key = "mcp"

    def __init__(
        self,
        connection_manager: McpManager | None = None,
        *,
        tool_service=None,
        config_service=None,
        agent_profiles=None,
        workspaces=None,
    ) -> None:
        self.connection_manager = connection_manager
        self._tool_service = tool_service
        self._config_service = config_service
        self._agent_profiles = agent_profiles
        self._workspaces = workspaces
        self._global_config: dict[str, Any] = {}
        self._agent_states: dict[str, _PreparedScope] = {}
        self._session_states: dict[str, _PreparedScope] = {}
        self._private_managers: dict[str, McpManager] = {}
        self._private_users: dict[str, set[str]] = {}
        self._state_lock = asyncio.Lock()

    async def start_and_register(self, raw_config: dict[str, Any]) -> None:
        """Load global MCP connections during Plugin startup."""
        self._global_config = _copy_entries(raw_config)
        if self.connection_manager is not None:
            await self.connection_manager.start_and_register(self._global_config)

    async def reload_and_register(
        self,
        raw_config: dict[str, Any],
        source: str = "config",
    ) -> None:
        """Reload global connections after ConfigService commits a snapshot."""
        self._global_config = _copy_entries(raw_config)
        if self.connection_manager is not None:
            await self.connection_manager.reload_and_register(self._global_config, source=source)

    async def stop(self) -> None:
        """Release scoped contributions before closing shared connections."""
        async with self._state_lock:
            for agent_id in tuple(self._agent_states):
                await self._dispose_scope_locked("agent", agent_id)
            for session_id in tuple(self._session_states):
                await self._dispose_scope_locked("session", session_id)
        if self.connection_manager is not None:
            await self.connection_manager.stop()

    def catalog(
        self,
        *,
        agent_id: str = "default",
        workspace: str | None = None,
        view: McpView = "effective",
    ) -> tuple[McpCatalogItem, ...]:
        """Return configuration-driven MCP entries without opening a connection."""
        if view not in {"effective", "sources"}:
            raise ValueError("view must be effective or sources")
        sources = self._sources(agent_id, workspace)
        names = sorted({name for entries in sources.values() for name in entries})
        items: list[McpCatalogItem] = []
        for name in names:
            present = [scope for scope in _SCOPES if name in sources[scope]]
            winner = next(scope for scope in _WINNER_ORDER if scope in present)
            for scope in _SCOPES:
                if scope not in present:
                    continue
                item = self._catalog_item(
                    name=name,
                    scope=scope,
                    raw=sources[scope][name],
                    effective=scope == winner,
                    shadowed_by=None if scope == winner else winner,
                    sources=sources,
                    agent_id=agent_id,
                    workspace=workspace,
                )
                if view == "sources" or item.effective:
                    items.append(item)
        return tuple(items)

    def diagnostics(self, *, workspace: str | None = None) -> tuple[str, ...]:
        """Return source-level project parsing errors without fake server rows."""
        reader = getattr(self._workspaces, "mcp_source_error", None)
        if not callable(reader) or not workspace:
            return ()
        try:
            error = reader(workspace)
        except Exception as exc:  # noqa: BLE001 external reader boundary
            error = f"无法读取项目 MCP 配置: {exc}"
        return (error,) if isinstance(error, str) and error else ()

    async def create(
        self,
        *,
        name: str,
        config: dict[str, Any],
        scope: McpScope,
        agent_id: str | None = None,
        workspace: str | None = None,
    ) -> McpCatalogItem:
        """Create one server in the explicitly selected source layer."""
        self._validate_server_name(name)
        self._validate_scope_context(scope, agent_id, workspace)
        self._validate_config(name, config)
        entries = self._source_for_write(scope, agent_id, workspace)
        if name in entries:
            raise ValueError(f"MCP server {name!r} already exists in {scope}")
        entries[name] = copy.deepcopy(config)
        await self._replace_source(scope, entries, agent_id, workspace)
        return self._source_item(name, scope, agent_id, workspace)

    async def update(
        self,
        *,
        name: str,
        patch: dict[str, Any],
        scope: McpScope,
        agent_id: str | None = None,
        workspace: str | None = None,
    ) -> McpCatalogItem:
        """Patch one layer only; inherited entries are never copied down."""
        self._validate_server_name(name)
        self._validate_scope_context(scope, agent_id, workspace)
        if not isinstance(patch, dict):
            raise TypeError("MCP patch must be an object")
        if "name" in patch:
            raise ValueError("renaming an MCP server is not supported")
        entries = self._source_for_write(scope, agent_id, workspace)
        current = entries.get(name)
        if not isinstance(current, dict):
            raise KeyError(f"MCP server {name!r} does not exist in {scope}")
        candidate = {**current, **copy.deepcopy(patch)}
        self._validate_config(name, candidate)
        entries[name] = candidate
        await self._replace_source(scope, entries, agent_id, workspace)
        return self._source_item(name, scope, agent_id, workspace)

    async def delete(
        self,
        *,
        name: str,
        scope: McpScope,
        agent_id: str | None = None,
        workspace: str | None = None,
    ) -> None:
        """Delete one server only from its explicit source layer."""
        self._validate_server_name(name)
        self._validate_scope_context(scope, agent_id, workspace)
        entries = self._source_for_write(scope, agent_id, workspace)
        if name not in entries:
            raise KeyError(f"MCP server {name!r} does not exist in {scope}")
        entries.pop(name)
        await self._replace_source(scope, entries, agent_id, workspace)

    async def prepare_view(
        self,
        agent_id: str,
        session_id: str,
        profile_config: Any | None = None,
    ) -> None:
        """Prepare Agent and Session MCP contributions for one ToolView."""
        if self._tool_service is None or not agent_id or not session_id:
            return
        workspace = await self._workspace_for_session(session_id)
        sources = self._sources(agent_id, workspace)
        agent_entries = sources["agent"]
        if not agent_entries:
            agent_entries = _profile_agent_overrides(profile_config, sources["global"])
        project_entries = sources["project"]
        async with self._state_lock:
            await self._prepare_scope_locked(
                kind="agent",
                key=agent_id,
                agent_id=agent_id,
                session_id=None,
                workspace=None,
                own_entries=agent_entries,
                lower_entries=sources["global"],
            )
            await self._prepare_scope_locked(
                kind="session",
                key=session_id,
                agent_id=agent_id,
                session_id=session_id,
                workspace=workspace,
                own_entries=project_entries,
                lower_entries={**sources["global"], **agent_entries},
            )

    async def _prepare_scope_locked(
        self,
        *,
        kind: Literal["agent", "session"],
        key: str,
        agent_id: str,
        session_id: str | None,
        workspace: str | None,
        own_entries: dict[str, Any],
        lower_entries: dict[str, Any],
    ) -> None:
        states = self._agent_states if kind == "agent" else self._session_states
        signature = _signature({"own": own_entries, "lower": lower_entries})
        current = states.get(key)
        if (
            current is not None
            and current.signature == signature
            and current.agent_id == agent_id
            and current.workspace == workspace
            and self._state_ready(current)
        ):
            return
        if current is not None:
            await self._dispose_scope_locked(kind, key)

        state = _PreparedScope(signature=signature, agent_id=agent_id, workspace=workspace)
        internal_scope = f"agent:{agent_id}" if kind == "agent" else f"session:{session_id}"
        owner = f"mcp:{kind}:{key}"
        inherited_limit = "global" if kind == "agent" else "agent"
        disabled_limit = "agent" if kind == "agent" else "session"

        # Publish the provisional state before doing any work so the same
        # disposer path can roll back partial registrations on failure or
        # cancellation.
        states[key] = state
        try:
            for name, raw in own_entries.items():
                if not isinstance(name, str):
                    continue
                lower = lower_entries.get(name, _MISSING)
                if raw == lower:
                    continue
                inspection = inspect_mcp_server(name, raw)
                known_names = self._known_tool_names(name)
                if inspection.config is not None and not inspection.disabled:
                    manager_key, manager = await self._manager_for_entry(name, raw)
                    if manager_key is not None:
                        self._private_users.setdefault(manager_key, set()).add(f"{kind}:{key}")
                        state.manager_refs[name] = (manager_key, manager)
                    tools = await build_mcp_tools_for_servers(manager, {name})
                    registered: list[str] = []
                    for tool in tools:
                        state.disposers.append(
                            self._tool_service.register(
                                tool,
                                owner=owner,
                                scope=internal_scope,
                                source="mcp",
                            )
                        )
                        registered.append(tool.name)
                    state.tool_names[name] = tuple(registered)
                    if lower is not _MISSING and known_names:
                        state.restrictions.append(
                            self._tool_service.restrict(
                                agent_id,
                                owner=owner,
                                deny=known_names,
                                session_id=session_id,
                                max_scope=inherited_limit,
                            )
                        )
                    continue

                # A disabled or invalid higher source intentionally blocks inherited
                # tools; it is never a silent fallback to a lower source.
                if lower is not _MISSING and known_names:
                    state.restrictions.append(
                        self._tool_service.restrict(
                            agent_id,
                            owner=owner,
                            deny=known_names,
                            session_id=session_id,
                            max_scope=disabled_limit,
                        )
                    )
        except BaseException:
            await self._dispose_scope_locked(kind, key)
            raise

    async def _dispose_scope_locked(self, kind: Literal["agent", "session"], key: str) -> None:
        states = self._agent_states if kind == "agent" else self._session_states
        state = states.pop(key, None)
        if state is None:
            return
        for restriction in state.restrictions:
            try:
                restriction()
            except Exception:
                logger.debug("MCP scope restriction cleanup failed", exc_info=True)
        for disposer in state.disposers:
            try:
                disposer()
            except Exception:
                logger.debug("MCP scope disposer cleanup failed", exc_info=True)
        for manager_key, manager in state.manager_refs.values():
            users = self._private_users.get(manager_key)
            if users is None:
                continue
            users.discard(f"{kind}:{key}")
            if users:
                continue
            self._private_users.pop(manager_key, None)
            self._private_managers.pop(manager_key, None)
            await manager.stop()

    async def _manager_for_entry(self, name: str, raw: Any) -> tuple[str | None, McpManager]:
        global_entry = self._global_source().get(name)
        if self.connection_manager is not None and raw == global_entry:
            return None, self.connection_manager
        manager_key = _signature({"name": name, "config": raw})
        manager = self._private_managers.get(manager_key)
        if manager is None:
            manager = McpManager(
                attachment_service=(
                    self.connection_manager.attachment_service
                    if self.connection_manager is not None
                    else None
                )
            )
            try:
                await manager.start_and_register({name: raw})
            except BaseException:
                try:
                    await manager.stop()
                except Exception:
                    logger.debug("MCP private manager rollback failed", exc_info=True)
                raise
            self._private_managers[manager_key] = manager
            self._private_users[manager_key] = set()
        return manager_key, manager

    def _state_ready(self, state: _PreparedScope) -> bool:
        return all(
            name in manager.get_connected_servers()
            for name, (_manager_key, manager) in state.manager_refs.items()
        )

    def _sources(self, agent_id: str, workspace: str | None) -> dict[McpScope, dict[str, Any]]:
        return {
            "global": self._global_source(),
            "agent": self._agent_source(agent_id),
            "project": self._project_source(workspace),
        }

    def _global_source(self) -> dict[str, Any]:
        snapshot = getattr(self._config_service, "snapshot", None)
        if callable(snapshot):
            try:
                value = snapshot().value
                raw = value.get("mcp", {}) if isinstance(value, dict) else {}
                if isinstance(raw, dict):
                    return _copy_entries(raw)
            except Exception:
                logger.debug("global MCP source snapshot failed", exc_info=True)
        return _copy_entries(self._global_config)

    def _agent_source(self, agent_id: str) -> dict[str, Any]:
        reader = getattr(self._agent_profiles, "mcp_source", None)
        if not callable(reader):
            return {}
        try:
            return _copy_entries(reader(agent_id))
        except Exception:
            logger.debug("agent MCP source read failed", exc_info=True)
            return {}

    def _project_source(self, workspace: str | None) -> dict[str, Any]:
        reader = getattr(self._workspaces, "mcp_source", None)
        if not callable(reader) or not workspace:
            return {}
        try:
            return _copy_entries(reader(workspace))
        except Exception:
            logger.debug("project MCP source read failed", exc_info=True)
            return {}

    async def _workspace_for_session(self, session_id: str) -> str | None:
        getter = getattr(self._workspaces, "get", None)
        if not callable(getter):
            return None
        try:
            result = getter(session_id)
            value = await result if inspect.isawaitable(result) else result
        except Exception:
            logger.debug("workspace lookup for session failed", exc_info=True)
            return None
        return value if isinstance(value, str) and value else None

    def _catalog_item(
        self,
        *,
        name: str,
        scope: McpScope,
        raw: Any,
        effective: bool,
        shadowed_by: McpScope | None,
        sources: dict[McpScope, dict[str, Any]],
        agent_id: str,
        workspace: str | None,
    ) -> McpCatalogItem:
        inspection = inspect_mcp_server(name, raw)
        if inspection.error:
            status: McpStatus = "invalid"
        elif inspection.disabled:
            status = "disabled"
        else:
            manager = self._catalog_manager(name, scope, raw, sources, agent_id, workspace)
            if manager is None:
                status = "configured"
            elif name in manager.get_connected_servers():
                status = "connected"
            elif scope == "global" or self._was_prepared(name, scope, agent_id, workspace):
                status = "failed"
            else:
                status = "configured"
        return McpCatalogItem(
            name=name,
            scope=scope,
            status=status,
            config=_sanitize_config(raw),
            effective=effective,
            shadowed_by=shadowed_by,
            error=inspection.error if status == "invalid" else ("连接失败" if status == "failed" else None),
            tools_count=self._tool_count(name, scope, agent_id, workspace),
        )

    def _catalog_manager(
        self,
        name: str,
        scope: McpScope,
        raw: Any,
        sources: dict[McpScope, dict[str, Any]],
        agent_id: str,
        workspace: str | None,
    ) -> McpManager | None:
        if scope == "global":
            return self.connection_manager
        lower = sources["global"].get(name, _MISSING)
        lower_scope: McpScope = "global"
        if scope == "project" and name in sources["agent"]:
            lower = sources["agent"][name]
            lower_scope = "agent"
        if raw == lower:
            return self._catalog_manager(
                name, lower_scope, lower, sources, agent_id, workspace
            )
        if scope == "agent":
            state = self._agent_states.get(agent_id)
            ref = state.manager_refs.get(name) if state else None
            return ref[1] if ref else None
        if scope == "project" and workspace:
            for state in self._session_states.values():
                if state.workspace != workspace or state.agent_id != agent_id:
                    continue
                ref = state.manager_refs.get(name)
                if ref:
                    return ref[1]
        return None

    def _was_prepared(
        self,
        name: str,
        scope: McpScope,
        agent_id: str,
        workspace: str | None,
    ) -> bool:
        if scope == "agent":
            state = self._agent_states.get(agent_id)
            return bool(state and name in state.tool_names)
        if scope == "project" and workspace:
            return any(
                state.agent_id == agent_id
                and state.workspace == workspace
                and name in state.tool_names
                for state in self._session_states.values()
            )
        return False

    def _tool_count(
        self,
        name: str,
        scope: McpScope,
        agent_id: str,
        workspace: str | None,
    ) -> int:
        if scope == "global" and self.connection_manager is not None:
            return sum(
                tool.startswith(f"{MCP_TOOL_PREFIX}{name}__")
                for tool in self.connection_manager.registered_tool_names
            )
        if scope == "agent":
            state = self._agent_states.get(agent_id)
            return len(state.tool_names.get(name, ())) if state else 0
        if scope == "project" and workspace:
            return sum(
                len(state.tool_names.get(name, ()))
                for state in self._session_states.values()
                if state.agent_id == agent_id and state.workspace == workspace
            )
        return 0

    def _known_tool_names(self, server_name: str) -> set[str]:
        prefix = f"{MCP_TOOL_PREFIX}{server_name}__"
        names: set[str] = set()
        if self.connection_manager is not None:
            names.update(
                name
                for name in self.connection_manager.registered_tool_names
                if name.startswith(prefix)
            )
        for state in (*self._agent_states.values(), *self._session_states.values()):
            names.update(state.tool_names.get(server_name, ()))
        return names

    def _source_for_write(
        self,
        scope: McpScope,
        agent_id: str | None,
        workspace: str | None,
    ) -> dict[str, Any]:
        if scope == "global":
            return self._global_source()
        if scope == "agent":
            return self._agent_source(agent_id or "")
        return self._project_source(workspace)

    async def _replace_source(
        self,
        scope: McpScope,
        entries: dict[str, Any],
        agent_id: str | None,
        workspace: str | None,
    ) -> None:
        if scope == "global":
            updater = getattr(self._config_service, "update", None)
            if callable(updater):
                result = updater({"mcp": entries})
                if inspect.isawaitable(result):
                    await result
            await self.reload_and_register(entries, source="api")
            return
        if scope == "agent":
            writer = getattr(self._agent_profiles, "replace_mcp_source", None)
            if not callable(writer):
                raise RuntimeError("AgentProfileService does not provide MCP source writes")
            result = writer(agent_id, entries)
            if inspect.isawaitable(result):
                await result
            return
        writer = getattr(self._workspaces, "replace_mcp_source", None)
        if not callable(writer):
            raise TypeError("WorkspaceService does not provide MCP source writes")
        result = writer(workspace, entries)
        if inspect.isawaitable(result):
            await result

    def _source_item(
        self,
        name: str,
        scope: McpScope,
        agent_id: str | None,
        workspace: str | None,
    ) -> McpCatalogItem:
        for item in self.catalog(
            agent_id=agent_id or "default",
            workspace=workspace,
            view="sources",
        ):
            if item.name == name and item.scope == scope:
                return item
        raise RuntimeError("written MCP source was not present in its catalog")

    @staticmethod
    def _validate_server_name(name: str) -> None:
        if not isinstance(name, str) or not _SAFE_SERVER_NAME.fullmatch(name):
            raise ValueError("MCP server name may only contain letters, digits, - and _")

    @staticmethod
    def _validate_scope_context(
        scope: str,
        agent_id: str | None,
        workspace: str | None,
    ) -> None:
        if scope not in _SCOPES:
            raise ValueError("scope must be global, agent, or project")
        if scope == "agent" and not agent_id:
            raise ValueError("agent scope requires agent_id")
        if scope == "project" and not workspace:
            raise ValueError("project scope requires workspace")

    @staticmethod
    def _validate_config(name: str, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise TypeError("MCP config must be an object")
        inspection = inspect_mcp_server(name, config)
        if inspection.error:
            raise ValueError(inspection.error)


def _copy_entries(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _signature(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _profile_value(profile: Any, field: str) -> Any:
    return profile.get(field) if isinstance(profile, dict) else getattr(profile, field, None)


def _profile_agent_overrides(profile: Any, global_entries: dict[str, Any]) -> dict[str, Any]:
    """Recover only differences for ephemeral profiles without source access."""
    raw = _profile_value(profile, "mcp_config")
    if not isinstance(raw, dict):
        return {}
    return {
        name: copy.deepcopy(value)
        for name, value in raw.items()
        if global_entries.get(name, _MISSING) != value
    }


def _sanitize_config(raw: Any) -> dict[str, Any]:
    """Expose useful metadata while redacting all credential-bearing values."""
    if not isinstance(raw, dict):
        return {"type": "unknown", "disabled": False}
    server_type = raw.get("type") or ("local" if "command" in raw else "unknown")
    value: dict[str, Any] = {
        "type": server_type,
        "disabled": bool(raw.get("disabled", False) or raw.get("enabled", True) is False),
        "timeout": raw.get("timeout", 30_000),
    }
    if isinstance(raw.get("command"), list):
        value["command"] = [part for part in raw["command"] if isinstance(part, str)]
    if isinstance(raw.get("url"), str):
        value["url"] = raw["url"]
    if isinstance(raw.get("environment"), dict):
        value["environment"] = {str(key): "***" for key in raw["environment"]}
    if isinstance(raw.get("headers"), dict):
        value["headers"] = {str(key): "***" for key in raw["headers"]}
    return value


__all__ = ["McpCatalogItem", "McpService"]
