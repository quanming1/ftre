import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ftre_agent import AgentConfig, AgentRegistry, LLMConfig
from ftre_agent_core.agent.runner import RunState, RunStatus
from ftre_agent_core.event import (
    ReplyEndEvent,
    ReplyFinishedReason,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from ftre_agent_core.message import Msg
from ftre_agent_runtime import AgentLoop, TurnExecutor
from ftre_agent_runtime.protocol import RuntimeInput

from ftre.services.messaging.bus import EventBus, MessageBusService
from ftre.services.session.events import SessionEventService
from ftre.services.session.projection import SessionProjection


class FakeAgent:
    def __init__(self, *, stream=False, fail_after_delta=False):
        self.stream = stream
        self.fail_after_delta = fail_after_delta
        self._captured_runtime_context = None
        self.tool_registry = Mock()
        self.run_state = RunState()
        self.run_state.done_reason = ReplyFinishedReason.COMPLETED
        self.run_state.status = RunStatus.COMPLETED
        self.run_state.iteration = 1
        self.run_state.token_usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }
        self.run_state.error = None
        self.run_state.error_code = None

    async def run(self, messages, runtime_context=None):
        self._captured_runtime_context = runtime_context
        if not self.stream:
            return
        reply_id = runtime_context["reply_id"]
        yield ReplyStartEvent(
            session_id="test-session",
            reply_id=reply_id,
            name="assistant",
        )
        yield TextBlockStartEvent(reply_id=reply_id, block_id="text-block")
        yield TextBlockDeltaEvent(
            reply_id=reply_id,
            block_id="text-block",
            delta="hello",
        )
        if self.fail_after_delta:
            raise RuntimeError("boom")
        yield TextBlockEndEvent(reply_id=reply_id, block_id="text-block")
        yield ReplyEndEvent(
            session_id="test-session",
            reply_id=reply_id,
            finished_reason=ReplyFinishedReason.COMPLETED,
        )

    def cancel_nowait(self):
        pass


def _make_executor(agent: FakeAgent) -> TurnExecutor:
    loop = object.__new__(AgentLoop)
    config = AgentConfig()
    config.llm = LLMConfig()
    loop._injected_config = config
    loop._event_loop = asyncio.get_running_loop()
    loop.hooks = None
    loop.agent_registry = AgentRegistry()
    loop.sessions = AsyncMock()
    loop.sessions.get_session = AsyncMock(
        return_value={"channel_id": "ws", "workspace": "/tmp"}
    )
    # F33：消息格式转换改为 SessionService 窄方法；fake 用真实实现保持 wire 保真。
    from ftre.services.session.message.converter import _as_msg, to_openai
    from ftre.services.session.message.multimodal import (
        build_user_content,
        normalize_stored_user_content,
    )

    loop.sessions.build_user_content = build_user_content
    loop.sessions.normalize_stored_user_content = normalize_stored_user_content
    loop.sessions.record_to_msg = _as_msg
    loop.sessions.to_openai_messages = (
        lambda records, *, vision: to_openai(list(records), config={"llm": {"vision": vision}})
    )
    loop.message_bus = MessageBusService(bus=AsyncMock(spec=EventBus))
    loop.message_bus.publish_outbound = AsyncMock()
    loop.agent_service = None
    loop.tools = SimpleNamespace(prepare_view=AsyncMock(return_value=Mock()))
    loop.profiles = SimpleNamespace(
        resolve_for_inbound=AsyncMock(return_value=SimpleNamespace(value=None))
    )
    loop.workspaces = SimpleNamespace(
        create_accessor=Mock(return_value=SimpleNamespace(get=lambda: "/tmp", set=lambda value: value)),
        ensure_extension_layout=AsyncMock(),
    )
    loop.config_service = None
    loop.tracer = Mock()

    loop.session_projection = SessionProjection(loop.sessions)
    loop.session_events = SessionEventService(
        SimpleNamespace(projection=loop.session_projection),
        loop.message_bus,
    )

    async def finish_open_replies(session_id, reason, *, error=None):
        return await loop.session_projection.finish_open(session_id, reason, error=error)

    loop.sessions.finish_open_replies.side_effect = finish_open_replies

    executor = TurnExecutor(
        loop,
        sessions=loop.sessions,
        agents=None,
        tools=loop.tools,
        profiles=loop.profiles,
        workspaces=loop.workspaces,
        config_service=None,
        llm_service=None,
    )
    executor._core_factory = Mock(return_value=agent)
    executor._build_messages = AsyncMock(
        return_value=([{"role": "user", "content": "hi"}], config)
    )
    executor._publish_session_status_async = AsyncMock()
    loop._executor = executor
    return executor


def _inbound():
    return RuntimeInput(
        session_id="test-session",
        request_id="request-test",
        channel_id="ws",
        content="hello",
        metadata={},
    )


async def _execute_admitted(executor, inbound=None):
    """Mirror the AgentLoop delivery boundary used by production code."""
    inbound = inbound or _inbound()
    turn_id = "turn_test"
    user_message_id = await executor._loop._persist_inbound_user_message(
        inbound,
        turn_id=turn_id,
    )
    return await executor.execute(
        inbound,
        turn_id=turn_id,
        user_message_id=user_message_id,
    )


def _saved_messages(executor):
    """所有通过 SessionProjection 持久化的消息。"""
    projected = [
        call.args[1]
        for call in executor._loop.sessions.upsert_message.call_args_list
    ]
    appended = [
        call.args[1]
        for call in executor._loop.sessions.save_message.call_args_list
    ]
    return projected + appended

def _updated_messages(executor):
    """所有通过 update_message 更新的消息。"""
    return [
        call.args[0]
        for call in executor._loop.sessions.update_message.call_args_list
    ]


@pytest.mark.asyncio
async def test_user_msg_is_persisted_before_agent_run():
    agent = FakeAgent()
    executor = _make_executor(agent)
    await _execute_admitted(executor)

    saved = _saved_messages(executor)
    assert len(saved) == 1
    assert isinstance(saved[0], Msg)
    assert saved[0].role == "user"
    assert saved[0].get_text_content() == "hello"
    assert agent._captured_runtime_context["reply_id"].startswith("turn_")


@pytest.mark.asyncio
async def test_turn_executor_does_not_drop_message_when_maintenance_is_external(caplog):
    agent = FakeAgent()
    executor = _make_executor(agent)

    with caplog.at_level("WARNING"):
        await _execute_admitted(executor)

    assert len(_saved_messages(executor)) == 1
    assert agent._captured_runtime_context is not None
    assert "正在压缩，丢弃新消息" not in caplog.text


@pytest.mark.asyncio
async def test_user_message_is_projected_before_frontend_echo():
    executor = _make_executor(FakeAgent())
    order: list[str] = []

    async def record_upsert(*args, **kwargs):
        order.append("persist")

    async def record_publish(message):
        if message.data.get("type") == "USER_MESSAGE":
            order.append("broadcast")

    executor._loop.sessions.upsert_message.side_effect = record_upsert
    executor._loop.message_bus.publish_outbound.side_effect = record_publish

    await _execute_admitted(executor)

    assert order == ["persist", "broadcast"]


@pytest.mark.asyncio
async def test_claimed_request_identity_is_persisted_on_user_message():
    executor = _make_executor(FakeAgent())
    inbound = _inbound()
    inbound = RuntimeInput(
        session_id="test-session",
        request_id="request-a",
        channel_id="ws",
        content="hello",
        metadata={"request_id": "request-a"},
    )

    await _execute_admitted(executor, inbound)

    user = _saved_messages(executor)[0]
    assert user.metadata["request_id"] == "request-a"


@pytest.mark.asyncio
async def test_channel_mismatch_is_failed_instead_of_false_completed():
    """防串台拒绝必须成为失败的 Turn 结果，不能伪装成 completed。"""
    executor = _make_executor(FakeAgent())
    inbound = _inbound()
    inbound = RuntimeInput(
        session_id="test-session",
        request_id="request-channel",
        channel_id="cron",
        content="hello",
    )

    error = await executor._loop._validate_inbound(
        RuntimeInput(
            session_id="test-session",
            request_id="request-channel",
            channel_id=inbound.channel_id,
            content="hello",
        )
    )

    assert error is not None
    assert error["code"] == "channel_mismatch"
    executor._core_factory.assert_not_called()


@pytest.mark.asyncio
async def test_turn_executor_has_no_critical_path_compaction_owner():
    executor = _make_executor(FakeAgent())

    await _execute_admitted(executor)

    executor._publish_session_status_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_decisions_use_selected_agent_context_window():
    executor = _make_executor(FakeAgent())
    coder_llm = LLMConfig(
        model="kiro/gpt-5.6-sol",
        context_window=1_050_000,
    )
    executor._loop.profiles.resolve_for_inbound.return_value = SimpleNamespace(value=SimpleNamespace(
        agent_id="coder",
        llm=coder_llm,
        agent_dir="",
        mcp_config={},
        tools_config=None,
        soul_prompt="",
        user_prompt_md="",
    ))
    inbound = RuntimeInput(
        session_id="test-session",
        request_id="request-coder",
        channel_id="ws",
        content="hello",
        metadata={"agent_id": "coder"},
    )

    await _execute_admitted(executor, inbound)

    decision_config = executor._build_messages.await_args.args[3]
    assert decision_config.llm.model == "kiro/gpt-5.6-sol"
    assert decision_config.llm.context_window == 1_050_000



@pytest.mark.asyncio
async def test_delta_is_live_only_and_reply_persists_as_one_msg():
    executor = _make_executor(FakeAgent(stream=True))
    await _execute_admitted(executor)

    saved = _saved_messages(executor)
    # user msg + assistant msg (REPLY_START 时 save)
    assert [message.role for message in saved] == ["user", "assistant"]

    # 最终状态通过 update_message 写入
    updated = _updated_messages(executor)
    assert len(updated) >= 1
    assistant_final = updated[-1]
    assert assistant_final.get_text_content() == "hello"
    assert assistant_final.finished_reason == ReplyFinishedReason.COMPLETED

    outbound = [
        call.args[0].data
        for call in executor._loop.message_bus.publish_outbound.call_args_list
        if call.args and getattr(call.args[0], "type", "") == "agent_event"
    ]
    assert any(frame.get("type") == "TEXT_BLOCK_DELTA" for frame in outbound)
    turn_end = next(
        frame
        for frame in outbound
        if frame.get("type") == "CUSTOM" and frame.get("name") == "TURN_END"
    )
    assert turn_end["value"]["success"] is True
    assert turn_end["value"]["reason"] == "completed"
    assert turn_end["value"]["iterations"] == 1
    assert turn_end["value"]["token_usage"]["total_tokens"] == 15

    pipeline_end = next(
        frame
        for frame in outbound
        if frame.get("type") == "CUSTOM" and frame.get("name") == "PIPELINE_END"
    )
    assert pipeline_end["value"]["success"] is True


@pytest.mark.asyncio
async def test_partial_reply_is_saved_as_error_msg():
    executor = _make_executor(FakeAgent(stream=True, fail_after_delta=True))
    await _execute_admitted(executor)

    saved = _saved_messages(executor)
    # user msg + assistant msg (REPLY_START 时 save)
    assert [message.role for message in saved] == ["user", "assistant"]

    # 异常终态通过 update_message 写入
    updated = _updated_messages(executor)
    assert len(updated) >= 1
    assistant_final = updated[-1]
    assert assistant_final.get_text_content() == "hello"
    assert assistant_final.finished_reason == ReplyFinishedReason.ERROR
    assert assistant_final.error == {"message": "Agent 执行异常", "code": "unknown"}
