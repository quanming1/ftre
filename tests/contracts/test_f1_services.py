from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter
from ftre_agent_core.tool import Tool, ToolRegistry

from ftre.services.config.service import ConfigConflictError, ConfigService
from ftre.services.filesystem.local import LocalFilesystemService
from ftre.services.filesystem.policy import PathPolicy, PathViolation
from ftre.services.http.service import HttpService
from ftre.services.system_prompt.service import SystemPromptService
from ftre.services.system_prompt.types import PromptSection
from ftre.services.tools.service import ToolService


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


def test_tool_scope_shadow_and_restriction() -> None:
    service = ToolService(ToolRegistry())
    global_tool = Tool(name="echo", description="global", func=lambda: "global")
    agent_tool = Tool(name="echo", description="agent", func=lambda: "agent")
    global_dispose = service.register(global_tool, owner="global", source="builtin")
    agent_dispose = service.register(agent_tool, owner="agent", scope="agent:worker", source="external:worker")
    assert service.snapshot("worker")[0].owner == "agent"
    restriction = service.restrict("worker", owner="policy", allow=["other"])
    assert service.snapshot("worker") == ()
    restriction()
    agent_dispose()
    assert service.snapshot("worker")[0].owner == "global"
    global_dispose()
