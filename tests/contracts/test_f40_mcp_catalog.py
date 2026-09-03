"""F40 contract coverage for MCP catalog, source writes and session ToolViews."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ftre_agent.tool import ToolDefinition

from ftre.plugins.builtin.mcp.router import build_router
from ftre.plugins.builtin.mcp.service import McpService
from ftre.services.config import ConfigService
from ftre.services.tools import ToolService


class _ConnectionManager:
    """Small connection double: catalog code only needs public manager methods."""

    attachment_service = None
    registered_tool_names = frozenset()

    def __init__(self, connected: tuple[str, ...] = ()) -> None:
        self.connected = list(connected)
        self.reloads: list[dict] = []

    async def start_and_register(self, raw):
        self.reloads.append(dict(raw))

    async def reload_and_register(self, raw, source="unknown"):
        del source
        self.reloads.append(dict(raw))

    async def stop(self):
        return None

    def get_connected_servers(self):
        return list(self.connected)


class _Profiles:
    def __init__(self, entries: dict[str, dict] | None = None) -> None:
        self.entries = entries or {}

    def mcp_source(self, agent_id: str):
        return dict(self.entries.get(agent_id, {}))

    def replace_mcp_source(self, agent_id: str, entries: dict):
        self.entries[agent_id] = dict(entries)
        return dict(entries)


class _Workspaces:
    def __init__(self, entries: dict[str, dict] | None = None) -> None:
        self.entries = entries or {}
        self.sessions: dict[str, str] = {}

    async def get(self, session_id: str):
        return self.sessions.get(session_id, "")

    def mcp_source(self, workspace: str):
        return dict(self.entries.get(workspace, {}))

    def replace_mcp_source(self, workspace: str, entries: dict):
        self.entries[workspace] = dict(entries)
        return dict(entries)

    def mcp_source_error(self, _workspace: str):
        return None


def _local(command: str, **extra):
    return {"type": "local", "command": [command], **extra}


def test_catalog_resolves_project_over_agent_over_global_and_redacts_secrets(tmp_path) -> None:
    workspace = str(tmp_path)
    config = ConfigService(
        tmp_path / "config.json",
        {
            "mcp": {
                "shared": _local("global", environment={"TOKEN": "secret"}),
                "global-only": _local("global-only"),
            }
        },
    )
    profiles = _Profiles(
        {
            "coder": {
                "shared": _local("agent"),
                "agent-only": _local("agent-only"),
            }
        }
    )
    workspaces = _Workspaces(
        {
            workspace: {
                "shared": _local("project"),
                "project-only": _local("project-only", headers={"Authorization": "secret"}),
            }
        }
    )
    service = McpService(
        _ConnectionManager(("global-only",)),
        config_service=config,
        agent_profiles=profiles,
        workspaces=workspaces,
    )

    effective = {item.name: item for item in service.catalog(agent_id="coder", workspace=workspace)}
    assert effective["shared"].scope == "project"
    assert effective["agent-only"].scope == "agent"
    assert effective["global-only"].scope == "global"
    assert effective["project-only"].scope == "project"

    sources = [
        item
        for item in service.catalog(
            agent_id="coder",
            workspace=workspace,
            view="sources",
        )
        if item.name == "shared"
    ]
    assert [(item.scope, item.effective, item.shadowed_by) for item in sources] == [
        ("global", False, "project"),
        ("agent", False, "project"),
        ("project", True, None),
    ]
    assert effective["project-only"].to_public_dict()["headers"] == {"Authorization": "***"}


def test_router_mutates_only_the_selected_source_layer(tmp_path) -> None:
    workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    config = ConfigService(tmp_path / "config.json", {})
    profiles = _Profiles()
    workspaces = _Workspaces()
    manager = _ConnectionManager()
    service = McpService(
        manager,
        config_service=config,
        agent_profiles=profiles,
        workspaces=workspaces,
    )
    app = FastAPI()
    app.include_router(build_router(service), prefix="/api")

    with TestClient(app) as client:
        global_response = client.post(
            "/api/mcp?scope=global",
            json={"name": "global-server", **_local("global", environment={"TOKEN": "secret"})},
        )
        assert global_response.status_code == 200
        assert global_response.json()["environment"] == {"TOKEN": "***"}

        agent_response = client.post(
            "/api/mcp?scope=agent&agent_id=coder",
            json={"name": "agent-server", **_local("agent")},
        )
        assert agent_response.status_code == 200
        assert "agent-server" in profiles.entries["coder"]

        project_response = client.post(
            f"/api/mcp?scope=project&workspace={workspace}",
            json={"name": "project-server", **_local("project")},
        )
        assert project_response.status_code == 200
        assert "project-server" in workspaces.entries[workspace]

        disabled = client.patch(
            f"/api/mcp/project-server?scope=project&workspace={workspace}",
            json={"disabled": True},
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "disabled"

        listed = client.get(
            f"/api/mcp?agent_id=coder&workspace={workspace}&view=sources"
        )
        assert listed.status_code == 200
        assert {server["scope"] for server in listed.json()["servers"]} == {
            "global",
            "agent",
            "project",
        }

        deleted = client.delete("/api/mcp/agent-server?scope=agent&agent_id=coder")
        assert deleted.status_code == 200
        assert "agent-server" not in profiles.entries["coder"]

    assert manager.reloads


@pytest.mark.asyncio
async def test_project_mcp_uses_session_scope_without_cross_session_leak(monkeypatch, tmp_path) -> None:
    global_tool = ToolDefinition(
        name="mcp__project__search",
        description="global",
        func=lambda: "global",
    )
    tools = ToolService()
    tools.register(global_tool, owner="mcp", source="mcp")

    workspaces = _Workspaces(
        {
            "workspace-a": {"project": _local("a")},
            "workspace-b": {"project": _local("b")},
        }
    )
    workspaces.sessions = {"session-a": "workspace-a", "session-b": "workspace-b"}
    profiles = _Profiles()

    async def fake_start(manager, raw):
        for name in raw:
            manager._connections[name] = SimpleNamespace(
                is_connected=True,
                disconnect=lambda: _done(),
            )

    async def _done():
        return None

    async def fake_build(_manager, names):
        return [
            ToolDefinition(
                name=f"mcp__{name}__search",
                description="project",
                func=lambda: "project",
            )
            for name in names
        ]

    monkeypatch.setattr(
        "ftre.plugins.builtin.mcp.connection.McpManager.start_and_register",
        fake_start,
    )
    monkeypatch.setattr(
        "ftre.plugins.builtin.mcp.service.build_mcp_tools_for_servers",
        fake_build,
    )

    service = McpService(
        _ConnectionManager(("project",)),
        tool_service=tools,
        config_service=ConfigService(
            tmp_path / "config.json",
            {"mcp": {"project": _local("global")}},
        ),
        agent_profiles=profiles,
        workspaces=workspaces,
    )
    await service.start_and_register({"project": _local("global")})
    await service.prepare_view("coder", "session-a")
    await service.prepare_view("coder", "session-b")

    item_a = tools.get("mcp__project__search", "coder", "session-a")
    item_b = tools.get("mcp__project__search", "coder", "session-b")
    assert item_a is not None and item_a.scope == "session:session-a"
    assert item_b is not None and item_b.scope == "session:session-b"
    other = tools.get("mcp__project__search", "coder", "other")
    assert other is not None and other.definition is global_tool
    await service.stop()


@pytest.mark.asyncio
async def test_prepare_view_rolls_back_partial_scope_on_failure(monkeypatch, tmp_path) -> None:
    tools = ToolService()
    profiles = _Profiles({"coder": {"private": _local("private")}})
    workspaces = _Workspaces()
    manager = _ConnectionManager()

    async def fake_start(private_manager, raw):
        for name in raw:
            private_manager._connections[name] = SimpleNamespace(
                is_connected=True,
                disconnect=lambda: _done(),
            )

    async def _done():
        return None

    async def fail_build(_manager, _names):
        raise RuntimeError("tool discovery failed")

    monkeypatch.setattr(
        "ftre.plugins.builtin.mcp.connection.McpManager.start_and_register",
        fake_start,
    )
    monkeypatch.setattr(
        "ftre.plugins.builtin.mcp.service.build_mcp_tools_for_servers",
        fail_build,
    )

    service = McpService(
        manager,
        tool_service=tools,
        config_service=ConfigService(tmp_path / "config.json", {"mcp": {}}),
        agent_profiles=profiles,
        workspaces=workspaces,
    )

    with pytest.raises(RuntimeError, match="tool discovery failed"):
        await service.prepare_view("coder", "session-1")

    assert service._agent_states == {}
    assert service._private_managers == {}
    assert service._private_users == {}
    assert tools.snapshot("coder", "session-1") == ()
    await service.stop()


@pytest.mark.asyncio
async def test_session_restriction_does_not_hide_another_session() -> None:
    tools = ToolService()
    global_dispose = tools.register(
        ToolDefinition(name="echo", description="global", func=lambda: "global"),
        owner="test",
    )
    session_dispose = tools.register(
        ToolDefinition(name="echo", description="session", func=lambda: "session"),
        owner="test",
        scope="session:one",
    )
    restriction = tools.restrict(
        "coder",
        owner="test",
        deny={"echo"},
        session_id="one",
        max_scope="session",
    )

    assert tools.get("echo", "coder", "one") is None
    assert tools.get("echo", "coder", "two") is not None
    restriction()
    assert tools.get("echo", "coder", "one").scope == "session:one"
    session_dispose()
    global_dispose()
