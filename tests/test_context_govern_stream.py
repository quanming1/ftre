import pytest
from cordis import Context

from ftre.features.context_govern import plugin as context_govern
from ftre.services.filesystem.local import LocalFilesystemService
from ftre.services.system_prompt.service import SystemPromptService
from ftre.services.workspace.service import WorkspaceService


async def _assemble(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Always verify the Msg boundary.", encoding="utf-8")
    prompts = SystemPromptService()
    root = Context()
    root.provide("system_prompt", prompts)
    root.provide("filesystem", LocalFilesystemService())
    root.provide("workspaces", WorkspaceService())
    fiber = root.plugin(context_govern.apply)
    await fiber
    return root, prompts.assemble("default", "session", workspace=str(tmp_path)), fiber


@pytest.mark.asyncio
async def test_context_govern_injects_workspace_agents_md(tmp_path):
    root, text, _ = await _assemble(tmp_path)
    try:
        assert "Always verify the Msg boundary." in text
    finally:
        cleanup = root.dispose()
        if cleanup is not None:
            await cleanup
