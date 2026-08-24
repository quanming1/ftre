from __future__ import annotations

import pytest

from ftre.app.gateway.composition import build_composition


@pytest.mark.asyncio
async def test_inbox_runtime_is_separate_from_business_tool_lifecycles(tmp_path) -> None:
    composition = await build_composition(
        {"sessions_dir": str(tmp_path / "sessions")},
    )
    try:
        tools = composition.context.tools
        queue_names = {"send_message", "task", "team_create", "team_add_agent", "team_say",
                       "team_agent_status", "team_delete", "wait_agent"}
        contributions = {item.name: item for item in tools.snapshot()}
        assert queue_names.issubset(contributions)
        assert contributions["send_message"].owner == "messaging"
        assert contributions["task"].owner == "task"
        assert {contributions[name].owner for name in queue_names - {"send_message", "task"}} == {"team"}

        assert await composition.plugins.restart("task") is True

        assert await composition.plugins.unload("task") is True
        after_task_unload = {item.name for item in tools.snapshot()}
        assert "task" not in after_task_unload
        assert {"send_message", "team_create", "wait_agent"}.issubset(after_task_unload)

        assert await composition.plugins.unload("team") is True
        assert await composition.plugins.unload("messaging") is True
        assert await composition.plugins.unload("inbox") is True
        assert "inbox" not in composition.context.reflect.store
        # 依赖方先卸载后，Inbox 才能安全关闭；三组业务 Tool 均已由各自 Owner 撤销。
        assert queue_names.isdisjoint({item.name for item in tools.snapshot()})

        assert await composition.plugins.restart("inbox") is False
    finally:
        await composition.close()
