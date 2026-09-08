"""Turn 状态机生命周期测试（SessionLog 事件流）。

验证：turn/start → 事件流 → turn/end 的完整生命周期，
以及 outcome 映射（completed/error/cancelled/paused）与 usage 汇总。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from ftre_agent import AgentConfig, LLMConfig
from ftre_agent.message import AssistantMsg
from ftre_agent.session import SessionLog
from ftre_agent.session.events import (
    AssistantChunk,
    AssistantChunkData,
    AssistantMessage,
    AssistantMessageData,
)
from ftre_agent.types import ReplyFinishedReason
from ftre_agent_runtime import TurnExecutor
from ftre_agent_runtime.protocol import RuntimeInput
from ftre_agent_runtime.run_state import RunState, RunStatus


class FakeSessions:
    """最小 SessionService 曦身：SessionLog 装配 + 事件记录。"""

    def __init__(self):
        self.logs: dict[str, SessionLog] = {}
        self.flush_log = AsyncMock()

    async def log(self, session_id: str) -> SessionLog:
        if session_id not in self.logs:
            self.logs[session_id] = SessionLog(session_id)
        return self.logs[session_id]

    async def append_event(self, session_id, type_, data, *, message_id=None):
        log = await self.log(session_id)
        return log.append(type_, data, message_id=message_id)


class FakeAgent:
    def __init__(self, *, events=None, status=RunStatus.COMPLETED,
                 done_reason=ReplyFinishedReason.COMPLETED):
        self._events = events or []
        self.run_state = RunState()
        self.run_state.done_reason = done_reason
        self.run_state.status = status
        self.run_state.iteration = 1
        self.run_state.token_usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }
        self.run_state.error = None
        self.run_state.error_code = None

    async def run(self, messages, runtime_context=None):
        for event in self._events:
            yield event


def _mk_harness(agent, model="test-model"):
    """构造 (executor, sessions)：executor.execute 直接驱动已构造的 FakeAgent。"""
    sessions = FakeSessions()
    loop = Mock()
    loop.sessions = sessions
    loop.agent_subject = lambda agent_id: Mock()

    async def append_session_event(session_id, event):
        await sessions.append_event(
            session_id, event.type, event.data,
            message_id=getattr(event, "message_id", None),
        )

    loop.append_session_event = append_session_event

    executor = TurnExecutor(
        loop,
        sessions=sessions,
        agents=Mock(),
        attachments=None,
        system_prompt=None,
        hooks=None,
        agent_registry=Mock(),
        tools=None,
        profiles=None,
        workspaces=None,
    )

    config = AgentConfig(llm=LLMConfig(provider="p", model=model, api_key="k", api_base=""))

    original_execute = executor.execute

    async def execute(inbound, **kwargs):
        # 拦截 _drive 前的 agent 注入：构造 Turn 后绕过 _build
        async def patched_run(msgs, runtime_context=None):
            async for event in agent.run(msgs, runtime_context=runtime_context):
                yield event

        original_build = executor._build

        async def build(turn):
            turn.agent = SimpleNamespace(
                run=patched_run,
                run_state=agent.run_state,
            )
            turn.messages = []
            turn.runtime_context = {
                "session_id": turn.session_id,
                "request_id": turn.inbound.request_id,
                "turn_id": turn.turn_id,
                "log_flush": sessions.flush_log,
            }
            from ftre_agent_runtime.state import TurnStatus

            return TurnStatus.RUNNING

        executor._build = build
        try:
            return await original_execute(inbound, config=config, **kwargs)
        finally:
            executor._build = original_build

    return SimpleNamespace(execute=execute, sessions=sessions)


def _runtime_input(session_id="ws_sess_t") -> RuntimeInput:
    return RuntimeInput(
        session_id=session_id,
        request_id="req_1",
        channel_id="ws",
        content="hello",
        source="user",
    )


def _chunk(delta: str, message_id: str = "m1") -> AssistantChunk:
    return AssistantChunk(
        data=AssistantChunkData(kind="text", delta=delta, block_id="b1"),
        message_id=message_id,
    )


def _assistant_message(message_id: str = "m1") -> AssistantMessage:
    msg = AssistantMsg(id=message_id, content="hello world")
    return AssistantMessage(
        data=AssistantMessageData(message=msg.model_dump(mode="json")),
        message_id=message_id,
    )


async def test_completed_turn_emits_start_events_end():
    events = [_chunk("hello"), _assistant_message()]
    harness = _mk_harness(FakeAgent(events=events))
    result = await harness.execute(_runtime_input(), turn_id="turn_t")

    log = harness.sessions.logs["ws_sess_t"]
    types = [e["type"] for e in log.events]
    assert types[0] == "turn/start"
    assert types[-1] == "turn/end"
    assert "assistant/chunk" in types
    assert "assistant/message" in types

    turn_end = log.events[-1]
    assert turn_end["data"]["outcome"] == "completed"
    assert turn_end["data"]["usage"]["total_tokens"] == 15
    assert turn_end["message_id"] == "m1"
    assert result.status == "completed"
    assert result.final_content == "hello world"


async def test_turn_start_carries_trigger_and_model():
    harness = _mk_harness(FakeAgent(events=[_assistant_message()]), model="deepseek-chat")
    await harness.execute(_runtime_input(), turn_id="turn_t")

    turn_start = harness.sessions.logs["ws_sess_t"].events[0]
    assert turn_start["type"] == "turn/start"
    assert turn_start["data"]["trigger"] == "user"
    assert turn_start["data"]["model"] == "deepseek-chat"
    assert turn_start["data"]["request_id"] == "req_1"


async def test_error_turn_outcome():
    agent = FakeAgent(events=[])
    agent.run_state.status = RunStatus.ERROR
    agent.run_state.done_reason = ReplyFinishedReason.ERROR
    agent.run_state.error = "LLM exploded"
    agent.run_state.error_code = "provider_error"
    harness = _mk_harness(agent)
    result = await harness.execute(_runtime_input(), turn_id="turn_t")

    turn_end = harness.sessions.logs["ws_sess_t"].events[-1]
    assert turn_end["type"] == "turn/end"
    assert turn_end["data"]["outcome"] == "error"
    assert turn_end["data"]["error"]["code"] == "provider_error"
    assert result.status == "failed"


async def test_cancelled_turn_outcome():
    agent = FakeAgent(events=[])
    agent.run_state.status = RunStatus.CANCELLED
    agent.run_state.done_reason = ReplyFinishedReason.INTERRUPTED
    harness = _mk_harness(agent)
    result = await harness.execute(_runtime_input(), turn_id="turn_t")

    turn_end = harness.sessions.logs["ws_sess_t"].events[-1]
    assert turn_end["data"]["outcome"] == "cancelled"
    assert result.status == "cancelled"


async def test_paused_turn_outcome():
    agent = FakeAgent(events=[])
    agent.run_state.status = RunStatus.PAUSED
    harness = _mk_harness(agent)
    result = await harness.execute(_runtime_input(), turn_id="turn_t")

    turn_end = harness.sessions.logs["ws_sess_t"].events[-1]
    assert turn_end["data"]["outcome"] == "paused"
    assert result.paused is True
