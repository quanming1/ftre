from __future__ import annotations

import asyncio

import pytest

from ftre.services.agent_loop.runtime.loop.completion_registry import CompletionRegistry


@pytest.mark.asyncio
async def test_completion_registry_close_wakes_waiters_and_clears_cache():
    registry = CompletionRegistry()
    waiter = asyncio.create_task(registry.wait("s1", "request-1"))
    await asyncio.sleep(0)

    await registry.complete(
        "s2",
        "request-2",
        type("Outcome", (), {"status": "completed"})(),
    )
    await registry.close()

    with pytest.raises(RuntimeError, match="AgentLoop 已关闭"):
        await waiter
    assert registry._waiters == {}
    assert registry._cache == {}
