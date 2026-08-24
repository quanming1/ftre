from __future__ import annotations

import pytest
from cordis import FiberState

from ftre.app.gateway.composition import build_composition


@pytest.mark.asyncio
async def test_feature_plugins_activate_through_cordis_composition():
    composition = await build_composition({})
    try:
        statuses = {item.id: item for item in composition.plugins.statuses()}
        expected = {
            "skill", "mcp", "plan", "messaging", "task", "team", "schedule",
            "context-govern", "session-title",
        }
        assert expected.issubset(statuses)
        assert all(statuses[name].state is FiberState.ACTIVE for name in expected)
        assert {"skills", "mcp", "schedule"}.issubset(
            composition.context.reflect.store
        )
    finally:
        await composition.close()
