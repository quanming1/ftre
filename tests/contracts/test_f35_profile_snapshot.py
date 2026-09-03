"""F35.3 AgentProfileService 来源优先级与快照契约。"""

from __future__ import annotations

import json
from pathlib import Path

from ftre.services.agent_profile import AgentManager, AgentProfileService, ProfileQuery


def _write_agent(root, name: str, workspace: str, soul: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "agent.config.json").write_text(
        json.dumps({"id": name, "workspace": workspace}), encoding="utf-8"
    )
    (directory / "SOUL.md").write_text(soul, encoding="utf-8")


def test_profile_snapshot_uses_project_before_user_and_is_hashed(tmp_path, monkeypatch) -> None:
    user_agents = tmp_path / "user" / ".ftre" / "agents"
    project_agents = tmp_path / "project" / ".ftre" / "agents"
    _write_agent(user_agents, "default", "user-workspace", "user soul")
    _write_agent(project_agents, "default", "project-workspace", "project soul")
    monkeypatch.setattr(
        "ftre.services.agent_profile.manager.load_config_file",
        lambda: {"providers": {}},
    )

    service = AgentProfileService(AgentManager(user_agents))
    snapshot = service.resolve_snapshot(
        ProfileQuery(
            name="default",
            project_root=str(tmp_path / "project"),
            user_root=str(tmp_path / "user" / ".ftre"),
            metadata={"source": "test"},
        )
    )

    assert snapshot.workspace == "project-workspace"
    assert snapshot.prompt_sources["SOUL.md"] == "project soul"
    assert Path(snapshot.source_trace[0]).parts[-4:] == ("project", ".ftre", "agents", "default")
    assert len(snapshot.snapshot_hash) == 64
    assert snapshot.to_agent_config().workspace == "project-workspace"
    assert snapshot.to_agent_config().llm.model == snapshot.llm.model
    try:
        snapshot.prompt_sources["SOUL.md"] = "mutate"
    except TypeError:
        pass
    else:  # pragma: no cover - MappingProxyType must reject mutation
        raise AssertionError("prompt snapshot is mutable")


def test_profile_snapshot_falls_back_to_host_manager(tmp_path, monkeypatch) -> None:
    agents = tmp_path / "agents"
    _write_agent(agents, "default", "host", "host soul")
    monkeypatch.setattr(
        "ftre.services.agent_profile.manager.load_config_file",
        lambda: {"providers": {}},
    )
    service = AgentProfileService(AgentManager(agents))
    snapshot = service.resolve(ProfileQuery(name="default"))
    assert snapshot.workspace == "host"
    assert snapshot.name == "default"


def test_profile_migration_has_no_legacy_agent_source_files() -> None:
    root = Path(__file__).parents[2]
    legacy = root / "src" / "ftre" / "services" / "agent"
    assert not list(legacy.rglob("*.py"))
    assert (root / "src" / "ftre" / "services" / "agent_profile" / "plugin.py").is_file()
