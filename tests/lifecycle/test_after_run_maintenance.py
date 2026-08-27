"""验证 agent/after-run 维护 Hook 的配置和 compacting 屏障接线。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from ftre_agent import (
    AGENT_AFTER_RUN_SPEC,
    AGENT_BEFORE_RUN_SPEC,
    AgentConfig,
    AgentRegistry,
    AgentRunResult,
    AllowRun,
)
from ftre_agent_runtime import AgentLoop
from ftre_agent_runtime.protocol import RuntimeInput


@pytest.mark.asyncio
async def test_after_run_wires_config_and_maintenance_barrier() -> None:
    """Compaction 执行期间，Inbox 必须仍把 Session 视为 busy。"""

    loop = object.__new__(AgentLoop)
    config = AgentConfig()
    loop._injected_config = config
    loop._event_loop = None
    loop._direct_tasks = {}
    loop._direct_signals = {}
    loop._direct_completion_events = {}
    loop._direct_parent_tasks = {}
    loop._direct_reservations = set()
    loop._maintenance = {}
    loop.agent_registry = AgentRegistry()
    loop._publish_session_status_async = AsyncMock()
    loop.completions = SimpleNamespace(complete=AsyncMock())
    loop._validate_inbound = AsyncMock(return_value=None)
    loop._persist_inbound_user_message = AsyncMock(return_value="user-1")

    executor = SimpleNamespace(
        resolve_inbound_config=AsyncMock(return_value=(config, None)),
        execute=AsyncMock(
            return_value=AgentRunResult(
                session_id="session-1", turn_id="turn-1", status="completed"
            )
        ),
    )
    loop._executor = executor

    async def dispatch(spec, payload, *, agent_id):
        del agent_id
        if spec is AGENT_BEFORE_RUN_SPEC:
            return AllowRun()
        assert spec is AGENT_AFTER_RUN_SPEC
        # 真实 Compaction Hook 会在这里调用该回调；确认它拿到的是本轮
        # 精确配置，而不是 None 或重新读取的全局配置。
        assert payload.config is config
        assert payload.set_maintenance is not None
        await payload.set_maintenance(True, "context compaction")
        assert loop.get_session_status("session-1") == "compacting"
        assert loop.is_active_session("session-1") is True
        await payload.set_maintenance(False, "")
        return None

    # 只替换 Hook 调度端，不构造新的 Dispatcher/Service Locator；这样测试
    # 直接覆盖 AgentLoop 到公开 AfterRunPayload 的接线。
    loop._dispatch_agent_hook = dispatch

    await loop.run_input(
        RuntimeInput(
            session_id="session-1",
            request_id="request-1",
            channel_id="ws",
            content="hello",
        )
    )

    assert loop._maintenance == {}
    assert loop.get_session_status("session-1") == "idle"
    loop._publish_session_status_async.assert_has_calls(
        [call("session-1", "running"), call("session-1", "compacting"), call("session-1", "idle")]
    )
