from types import SimpleNamespace

import pytest

from ftre.plugin.builtin.context_govern import ContextGovernPlugin


@pytest.mark.asyncio
async def test_context_govern_keeps_msg_history_and_injects_agents_md(tmp_path):
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text("Always verify the Msg boundary.", encoding="utf-8")
    messages = [{"id": "msg-1", "role": "user", "content": []}]
    config = SimpleNamespace(system_prompt="base")
    context = SimpleNamespace(
        messages=messages,
        workspace=str(tmp_path),
        agent_dir="",
        config=config,
    )

    result = await ContextGovernPlugin()._govern(context)

    assert result.messages is messages
    assert "Always verify the Msg boundary." in result.config.system_prompt
