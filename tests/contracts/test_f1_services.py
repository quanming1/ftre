from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from ftre_agent.tool import ToolDefinition

from ftre.services.config.service import ConfigConflictError, ConfigService
from ftre.services.filesystem.local import LocalFilesystemService
from ftre.services.filesystem.policy import PathPolicy, PathViolation
from ftre.services.http.service import HttpService
from ftre.services.messaging.bus import BusMessage, EventBus, MessageBusService
from ftre.services.system_prompt.service import SystemPromptService
from ftre.services.system_prompt.types import PromptSection
from ftre.services.tools.service import ToolService
from ftre.services.workspace import WorkspaceService


@pytest.mark.asyncio
async def test_config_revision_atomic_update_and_watch(tmp_path: Path) -> None:
    service = ConfigService(tmp_path / "config.json", {"providers": {"demo": {"enabled": True}}})
    revisions: list[int] = []
    service.watch(lambda snapshot: revisions.append(snapshot.revision))
    snapshot = await service.update({"servers": {"gateway": {"port": 1234}}}, expected_revision=0)
    assert snapshot.revision == 1
    assert service.snapshot().value["providers"]["demo"]["enabled"] is True
    assert revisions == [1]
    with pytest.raises(ConfigConflictError):
        await service.update({}, expected_revision=0)


def test_filesystem_policy_and_atomic_write(tmp_path: Path) -> None:
    service = LocalFilesystemService()
    policy = PathPolicy(root=tmp_path)
    target = service.resolve("a.txt", tmp_path, policy)
    service.write_text_atomic(target, "hello")
    assert service.read_text(target) == "hello"
    with pytest.raises(PathViolation):
        service.resolve("..", tmp_path, policy)
    assert service.read_text(target, limit=3) == "hel"

    original = target.path.read_text(encoding="utf-8")
    replace = os.replace
    try:
        os.replace = lambda *_args: (_ for _ in ()).throw(OSError("simulated replace failure"))
        with pytest.raises(OSError):
            service.write_text_atomic(target, "new")
    finally:
        os.replace = replace
    assert target.path.read_text(encoding="utf-8") == original

    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target.path)
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(PathViolation):
            service.resolve(link, tmp_path, PathPolicy(root=tmp_path / "nested"))


@pytest.mark.asyncio
async def test_workspace_service_persists_session_workspace(tmp_path: Path) -> None:
    class Sessions:
        def __init__(self):
            self.workspace = str(tmp_path)

        async def get_session(self, _session_id):
            return {"workspace": self.workspace}

        async def update_session(self, _session_id, title=None, workspace=None):
            if workspace is not None:
                self.workspace = workspace

    sessions = Sessions()
    service = WorkspaceService(sessions)
    before = await service.get("session")
    changed = await service.set("session", str(tmp_path))
    assert changed == {"before": before, "after": str(tmp_path.resolve())}
    policy = await service.policy("session")
    assert policy.root == tmp_path.resolve()


def test_http_registry_freeze_marks_restart_required() -> None:
    service = HttpService()
    router = APIRouter()

    @router.get("/demo")
    async def demo():
        return {"ok": True}

    dispose = service.register_router(router, owner="demo")
    assert service.snapshot()[0]["owner"] == "demo"
    service.freeze()
    dispose()
    assert service.restart_required is True


def test_prompt_receipt_matches_assembly() -> None:
    service = SystemPromptService()
    service.register_section(PromptSection(name="base", content="base", owner="system"))
    service.register_section(PromptSection(name="agent", content="agent", scope="agent:worker", owner="worker"))
    assert service.assemble("worker", "session") == "base\n\nagent"
    receipt = service.receipt("worker", "session")
    assert [item["name"] for item in receipt.sections] == ["base", "agent"]


def test_system_prompt_service_owns_profile_and_runtime_sections() -> None:
    service = SystemPromptService()
    service.register_section(PromptSection(name="feature", content="feature"))
    profile = SimpleNamespace(
        soul_prompt="coding persona",
        user_prompt_md="user preferences",
        agent_dir="C:/agents/coder",
    )
    config = SimpleNamespace(llm=SimpleNamespace(vision=True))

    assembly = service.assemble_result(
        "coder",
        "session-1",
        workspace="repo",
        base_prompt="base",
        profile=profile,
        channel_id="ws",
        config=config,
    )

    names = [item.name for item in assembly.contributions]
    assert names == [
        "config-base",
        "profile-soul",
        "profile-user",
        "runtime-facts",
        "feature",
    ]
    assert assembly.text.count("coding persona") == 1
    assert assembly.text.count("user preferences") == 1
    assert assembly.text.count("<FTRE_SYSTEM_FACT>") == 1
    assert "vision=true" in assembly.text
    assert 'path="C:/agents/coder/SOUL.md"' in assembly.text

    receipt = service.receipt(
        "coder",
        "session-1",
        workspace="repo",
        base_prompt="base",
        profile=profile,
        channel_id="ws",
        config=config,
    )
    assert [item["name"] for item in receipt.sections] == names


def test_tool_scope_shadow_and_restriction() -> None:
    service = ToolService()
    global_tool = ToolDefinition(name="echo", description="global", func=lambda: "global")
    agent_tool = ToolDefinition(name="echo", description="agent", func=lambda: "agent")
    global_dispose = service.register(global_tool, owner="global", source="builtin")
    agent_dispose = service.register(agent_tool, owner="agent", scope="agent:worker", source="external:worker")
    assert service.snapshot("worker")[0].owner == "agent"
    restriction = service.restrict("worker", owner="policy", allow=["other"])
    assert service.snapshot("worker") == ()
    restriction()
    agent_dispose()
    assert service.snapshot("worker")[0].owner == "global"
    global_dispose()


@pytest.mark.asyncio
async def test_message_bus_service_forwards_inbound_publicly() -> None:
    service = MessageBusService(EventBus())
    message = BusMessage(type="user_message", data={"content": "cron"})
    await service.publish_inbound(message)

    received = await anext(service.bus.subscribe_inbound())
    assert received.id == message.id
