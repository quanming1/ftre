from __future__ import annotations

import pytest
from ftre_agent_core.tool import ToolRegistry

from cordis import Context, FiberState
from ftre.platform.plugin_runtime import PluginManager
from ftre.services.system_prompt import SystemPromptService
from ftre.services.tools import ToolService


@pytest.mark.asyncio
async def test_synthetic_plugin_uses_only_public_contracts() -> None:
    ctx = Context()
    tools = ToolService(ToolRegistry())
    prompts = SystemPromptService()
    ctx.provide("tools", tools)
    ctx.provide("system_prompt", prompts)
    manager = PluginManager(ctx)
    statuses = await manager.load(
        [],
        {"plugins": [{"id": "synthetic-audit", "entry": "tests.architecture.fixtures.audit_plugin:apply"}]},
    )
    assert statuses[0].state is FiberState.ACTIVE
    assert any(section.owner == "synthetic-audit" for section in prompts.snapshot())
    await manager.close()
    assert not prompts.snapshot()

