import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
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

from ftre.services.agent.config import AgentConfig, LLMConfig
from ftre.services.agent.contracts import InboundMessage
from ftre.services.agent.registry import AgentRegistry
from ftre.services.agent.runtime.engine import AgentLoop
from ftre.services.agent.runtime.turn_executor import TurnExecutor
from ftre.services.messaging.bus import BusMessage, InboundMetadata
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
    loop.session_manager = AsyncMock()
    loop.session_manager.get_session = AsyncMock(
        return_value={"channel_id": "ws", "workspace": "/tmp"}
    )
    loop.bus = AsyncMock()
    loop.agent_manager = Mock()
    loop.agent_service = None
    loop.mcp_service = None
    loop.tool_service = None
    loop.inbox = None
    loop._agent_created_emitted = set()
    loop.agent_manager.load = Mock(return_value=None)
    loop.agent_manager.create_agent = Mock(return_value=agent)
    loop.channel_manager = None
    loop.tool_registry = None
    loop.command_manager = Mock()
    loop.command_manager.try_dispatch_system = AsyncMock(return_value=False)
    loop.command_manager.match = Mock(return_value=None)
    loop.command_manager.match_any = Mock(return_value=None)
    loop.command_manager.try_dispatch = AsyncMock(return_value=None)
    loop.tracer = Mock()

    loop.session_projection = SessionProjection(loop.session_manager)
    loop.session_events = SessionEventService(
        SimpleNamespace(projection=loop.session_projection),
        loop.bus,
    )

    executor = TurnExecutor(loop, sessions=loop.session_manager)
    executor._build_messages = AsyncMock(
        return_value=([{"role": "user", "content": "hi"}], config)
    )
    executor._publish_session_status_async = AsyncMock()
    loop._executor = executor
    return executor


def _inbound():
    return BusMessage(
        type="user_message",
        from_channel="ws",
        to_channel="ws",
        from_session="test-session",
        to_session="test-session",
        data={"content": "hello", "session_id": "test-session"},
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
        for call in executor._loop.session_manager.upsert_message.call_args_list
    ]
    appended = [
        call.args[1]
        for call in executor._loop.session_manager.save_message.call_args_list
    ]
    return projected + appended

def _updated_messages(executor):
    """所有通过 update_message 更新的消息。"""
    return [
        call.args[0]
        for call in executor._loop.session_manager.update_message.call_args_list
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

    executor._loop.session_manager.upsert_message.side_effect = record_upsert
    executor._loop.bus.publish_outbound.side_effect = record_publish

    await _execute_admitted(executor)

    assert order == ["persist", "broadcast"]


@pytest.mark.asyncio
async def test_claimed_request_identity_is_persisted_on_user_message():
    executor = _make_executor(FakeAgent())
    inbound = _inbound()
    inbound.metadata = InboundMetadata(
        request_id="request-a",
    )

    await _execute_admitted(executor, inbound)

    user = _saved_messages(executor)[0]
    assert user.metadata["request_id"] == "request-a"


@pytest.mark.asyncio
async def test_channel_mismatch_is_failed_instead_of_false_completed():
    """防串台拒绝必须成为失败 TurnOutcome，不能伪装成 completed。"""
    executor = _make_executor(FakeAgent())
    inbound = _inbound()
    inbound.from_channel = "cron"

    error = await executor._loop._validate_inbound(
        InboundMessage(
            session_id="test-session",
            request_id="request-channel",
            channel_id=inbound.from_channel,
            content="hello",
        )
    )

    assert error is not None
    assert error["code"] == "channel_mismatch"
    executor._loop.agent_manager.create_agent.assert_not_called()


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
    executor._loop.agent_manager.load.return_value = SimpleNamespace(
        llm=coder_llm,
        agent_dir="",
    )
    inbound = _inbound()
    inbound.metadata = InboundMetadata(agent_id="coder")

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
        for call in executor._loop.bus.publish_outbound.call_args_list
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
