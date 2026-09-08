from __future__ import annotations

from types import SimpleNamespace

import pytest
from ftre_agent import AgentConfig, ContextBuildResult
from ftre_agent.message import UserMsg
from ftre_agent_runtime.executors.reasoning import ReasoningExecutor
from ftre_agent_runtime.protocol import RuntimeInput
from ftre_agent_runtime.run_state import RunState
from ftre_agent_runtime.state import Turn
from ftre_agent_runtime.turn_executor import TurnExecutor


class _Dispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, _spec, payload, *, context=None):
        del context
        self.calls += 1
        payload.messages[0].content[0].text = "插件只改了副本"
        return ContextBuildResult(payload.messages)


@pytest.mark.asyncio
async def test_context_build_receives_deep_copy_and_keeps_agent_state_intact():
    source = UserMsg(content="原始消息")
    agent = SimpleNamespace(
        model="test-model",
        system_prompt="",
        state=SimpleNamespace(context=[source]),
        tool_view=SimpleNamespace(to_openai_tools=list),
    )
    state = RunState(
        turn_id="turn-1",
        runtime_context={
            "session_id": "session-1",
            "request_id": "request-1",
            "context_limit": 1000,
        },
        iteration=2,
    )
    dispatcher = _Dispatcher()
    executor = ReasoningExecutor(
        agent,
        state,
        SimpleNamespace(model="test-model"),
        dispatcher,
    )

    view = await executor._build_context_view()

    assert dispatcher.calls == 1
    assert view[0].get_text_content() == "插件只改了副本"
    assert source.get_text_content() == "原始消息"
    assert view[0] is not source


@pytest.mark.asyncio
async def test_run_error_recovery_refreshes_context_after_plugin_persists_marker():
    persisted = UserMsg(content="压缩后的完整历史")

    class _Sessions:
        async def get_full_messages(self, _session_id):
            return [persisted]

    executor = object.__new__(TurnExecutor)
    executor._sessions = _Sessions()
    agent = SimpleNamespace(state=SimpleNamespace(context=[]))
    turn = SimpleNamespace(
        agent=agent,
        session_id="session-1",
        messages=[],
    )

    await executor._refresh_agent_context(turn)

    assert turn.messages[0].get_text_content() == "压缩后的完整历史"
    assert agent.state.context[0].get_text_content() == "压缩后的完整历史"
    assert agent.state.context[0] is not persisted


@pytest.mark.asyncio
async def test_turn_scope_uses_session_agent_when_inbound_metadata_is_empty():
    class _Sessions:
        async def get_session(self, _session_id):
            return {"agent_id": "coder"}

    class _Profiles:
        async def resolve_for_inbound(self, agent_id, _session_id, *, metadata):
            assert agent_id == "coder"
            assert metadata == {}

    executor = object.__new__(TurnExecutor)
    executor._sessions = _Sessions()
    executor._profiles = _Profiles()
    executor._load_current_config = lambda: AgentConfig()
    turn = Turn(
        turn_id="turn-1",
        inbound=RuntimeInput(
            session_id="session-1",
            request_id="request-1",
            channel_id="ws",
            content="hello",
            source="user",
        ),
        session_id="session-1",
    )

    await executor._resolve_turn_config(turn)

    assert turn.resolved_agent_id == "coder"
    assert executor._agent_id_for_turn(turn) == "coder"


@pytest.mark.asyncio
async def test_pre_resolved_turn_keeps_session_agent_scope_without_metadata():
    class _Sessions:
        async def get_session(self, _session_id):
            return {"agent_id": "coder"}

    executor = object.__new__(TurnExecutor)
    executor._sessions = _Sessions()
    turn = Turn(
        turn_id="turn-1",
        inbound=RuntimeInput(
            session_id="session-1",
            request_id="request-1",
            channel_id="ws",
            content="hello",
            source="user",
        ),
        session_id="session-1",
        config=AgentConfig(),
    )

    await executor._resolve_turn_config(turn)

    assert turn.resolved_agent_id == "coder"
    assert executor._agent_id_for_turn(turn) == "coder"


@pytest.mark.asyncio
async def test_lifecycle_agent_scope_resolver_uses_session_agent_without_metadata():
    class _Sessions:
        async def get_session(self, _session_id):
            return {"agent_id": "coder"}

    executor = object.__new__(TurnExecutor)
    executor._sessions = _Sessions()
    inbound = RuntimeInput(
        session_id="session-1",
        request_id="request-1",
        channel_id="ws",
        content="hello",
        source="user",
    )

    assert await executor.resolve_inbound_agent_id(inbound) == "coder"
