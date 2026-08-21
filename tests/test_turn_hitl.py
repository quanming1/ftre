"""HITL（工具确认）端到端集成测试。

覆盖 TurnExecutor 两条权限路径：
- 挂起：agent.run() 因工具命中 ASK 提前结束（RunStatus.PAUSED、done_reason=None），
  应发 success 的 TURN_END(reason=paused)，不产 error，Turn 正常收尾。
- 恢复：/allow、/deny 是不持久化的控制指令，指令合成
  UserConfirmResultEvent，TurnExecutor 落盘后驱动 Agent 继续。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cordis import Context
from ftre_agent_core.agent.runner import RunState, RunStatus
from ftre_agent_core.event import (
    ReplyFinishedReason,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from ftre_agent_core.message import (
    AssistantMsg,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
)

from ftre.platform.hooks import HookRuntime
from ftre.services.agent.config import AgentConfig, ContextConfig, LLMConfig
from ftre.services.agent.registry import AgentRegistry
from ftre.services.agent_loop.runtime.loop.engine import AgentLoop
from ftre.services.agent_loop.runtime.loop.turn_executor import TurnExecutor
from ftre.services.command import CommandService
from ftre.services.command.builtin import register_builtin_commands
from ftre.services.messaging.bus import BusMessage
from ftre.services.session.projection import SessionProjection
from ftre.services.system_prompt.hooks import (
    SYSTEM_PROMPT_ASSEMBLE_SPEC,
    PromptAssemblyPayload,
)


class PausingAgent:
    """模拟工具命中 ASK：产出 RequireUserConfirmEvent 后停在 PAUSED，不 finalize。"""

    def __init__(self):
        self.tool_registry = Mock()
        self._captured_runtime_context = None
        self._captured_run_input = None
        self.run_state = RunState()
        self.run_state.status = RunStatus.PAUSED
        self.run_state.done_reason = None
        self.run_state.iteration = 1
        self.run_state.token_usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }
        self.run_state.error = None
        self.run_state.error_code = None

    async def run(self, run_input, runtime_context=None):
        self._captured_runtime_context = runtime_context
        self._captured_run_input = run_input
        reply_id = runtime_context["reply_id"]
        yield RequireUserConfirmEvent(
            reply_id=reply_id,
            tool_call_id="call-1",
            tool_call_name="bash",
            arguments={"command": "ls"},
            reason="bash needs confirmation",
            rule_id="default-bash-ask",
        )

    def cancel_nowait(self):
        pass


class ResumingAgent:
    """模拟确认后恢复：正常跑完一轮，捕获注入的 state 与 run 输入。"""

    def __init__(self, state=None):
        self.tool_registry = Mock()
        self.state = state
        self._captured_runtime_context = None
        self._captured_run_input = None
        self.run_state = RunState()
        self.run_state.status = RunStatus.COMPLETED
        self.run_state.done_reason = ReplyFinishedReason.COMPLETED
        self.run_state.iteration = 2
        self.run_state.token_usage = {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "total_tokens": 28,
        }
        self.run_state.error = None
        self.run_state.error_code = None

    async def run(self, run_input, runtime_context=None):
        self._captured_runtime_context = runtime_context
        self._captured_run_input = run_input
        return
        yield  # pragma: no cover — 使其成为 async generator

    def cancel_nowait(self):
        pass


def _make_executor(agent) -> TurnExecutor:
    loop = object.__new__(AgentLoop)
    config = AgentConfig()
    config.llm = LLMConfig()
    config.context = ContextConfig()
    loop._injected_config = config
    loop._event_loop = asyncio.get_running_loop()
    loop.hooks = None
    loop.agent_registry = AgentRegistry()
    loop.session_manager = AsyncMock()
    loop.session_manager.get_session = AsyncMock(
        return_value={"channel_id": "ws", "workspace": "/tmp"}
    )
    loop.session_manager.get_messages_by_session = AsyncMock(return_value=[])
    loop.session_manager.get_context_messages = AsyncMock(return_value=[])
    loop.bus = AsyncMock()
    loop.agent_manager = Mock()
    loop.agent_service = None
    loop._agent_created_emitted = set()
    loop.agent_manager.load = Mock(return_value=None)
    loop.agent_manager.create_agent = Mock(return_value=agent)
    loop.agent_manager._default_agent_state = Mock(return_value=_FakeState())
    loop.channel_manager = None
    loop.tool_registry = None
    loop.compact_manager = AsyncMock()
    loop.compact_manager.is_compacting = Mock(return_value=False)
    loop.compact_manager.should_compact = AsyncMock(return_value=False)
    loop.command_manager = Mock()
    loop.command_manager.try_dispatch_system = AsyncMock(return_value=False)
    loop.command_manager.match = Mock(return_value=None)
    loop.command_manager.match_any = Mock(return_value=None)
    loop.command_manager.try_dispatch = AsyncMock(return_value=None)
    loop.tracer = Mock()

    loop.session_projection = SessionProjection(loop.session_manager)

    async def emit_session_event(session_id, channel_id, event, *, metadata=None):
        return await AgentLoop.emit_session_event(
            loop,
            session_id,
            channel_id,
            event,
            metadata=metadata,
        )

    loop.emit_session_event = emit_session_event
    executor = TurnExecutor(loop, sessions=loop.session_manager)
    executor._build_messages = AsyncMock(
        return_value=([{"role": "user", "content": "hi"}], config)
    )
    executor._publish_session_status_async = AsyncMock()
    loop._executor = executor
    return executor


class _FakeState:
    """占位 AgentState：仅需 context 可赋值。"""

    def __init__(self):
        self.context = []


def _user_inbound():
    return BusMessage(
        type="user_message",
        from_channel="ws",
        to_channel="ws",
        from_session="test-session",
        to_session="test-session",
        data={"content": "run ls", "session_id": "test-session"},
        metadata={},
    )


def _confirm_inbound(*, approved=True, tool_call_id="call-1"):
    return BusMessage(
        type="user_message",
        from_channel="ws",
        to_channel="ws",
        from_session="test-session",
        to_session="test-session",
        data={
            "session_id": "test-session",
            "content": (
                f"/allow {tool_call_id}" if approved else f"/deny {tool_call_id}"
            ),
        },
        metadata={},
    )


def _enable_builtin_commands(executor):
    service = CommandService()

    async def resume_confirmation(session_id, channel_id, events, metadata):
        for event in events:
            await executor._loop.emit_session_event(
                session_id, channel_id, event, metadata=metadata
            )
        inbound = _confirm_inbound()
        return await executor.execute(
            inbound,
            confirm_event=events[-1],
            persist_input=False,
        )

    agents = SimpleNamespace(resume_confirmation=resume_confirmation)
    sessions = executor._loop.session_manager
    compaction = SimpleNamespace()
    register_builtin_commands(
        service.runtime,
        agents=agents,
        sessions=sessions,
        compaction=compaction,
    )
    executor._loop.command_service = service
    return service


async def _execute_command(executor, inbound):
    """Exercise the same ingress parse/dispatch path used by AgentLoop."""
    service = executor._loop.command_service
    command = service.parse({"inbound": inbound})
    return await service.dispatch_inbound(inbound, definition=command)


def _saved_messages(executor):
    projected = [
        call.args[1]
        for call in executor._loop.session_manager.upsert_message.call_args_list
    ]
    appended = [
        call.args[1]
        for call in executor._loop.session_manager.save_message.call_args_list
    ]
    return projected + appended


def _outbound_frames(executor):
    return [
        call.args[0].data
        for call in executor._loop.bus.publish_outbound.call_args_list
        if call.args and getattr(call.args[0], "type", "") == "agent_event"
    ]


@pytest.mark.asyncio
async def test_tool_ask_pauses_turn_with_success_turn_end():
    """工具命中 ASK → PAUSED：发 success 的 TURN_END(reason=paused)，不产 error。"""
    agent = PausingAgent()
    executor = _make_executor(agent)

    await executor.execute(_user_inbound())

    frames = _outbound_frames(executor)

    # 产出了 REQUIRE_USER_CONFIRM 事件（转发给前端）
    require = next(f for f in frames if f.get("type") == "REQUIRE_USER_CONFIRM")
    assert require["tool_call_id"] == "call-1"
    assert require["tool_call_name"] == "bash"

    # TURN_END 是 success 且 reason=paused，不是 error
    turn_end = next(
        f for f in frames if f.get("type") == "CUSTOM" and f.get("name") == "TURN_END"
    )
    assert turn_end["value"]["success"] is True
    assert turn_end["value"]["reason"] == "paused"

    # PIPELINE_END success（Turn 正常收尾，不是异常）
    pipeline_end = next(
        f
        for f in frames
        if f.get("type") == "CUSTOM" and f.get("name") == "PIPELINE_END"
    )
    assert pipeline_end["value"]["success"] is True

    # TurnExecutor 不再维护 session 全局 active 集合；SessionLane 持有执行所有权。


@pytest.mark.asyncio
async def test_confirm_command_is_not_persisted_as_user_msg():
    """/allow 命中不持久化指令，不产生 UserMsg。"""
    agent = ResumingAgent()
    executor = _make_executor(agent)
    _enable_builtin_commands(executor)
    executor._loop.session_manager.get_messages_by_session = AsyncMock(
        return_value=[
            AssistantMsg(
                id="turn_paused",
                content=[
                    ToolCallBlock(
                        id="call-1",
                        name="bash",
                        arguments={"command": "ls"},
                        state=ToolCallState.ASKING,
                    )
                ],
            )
        ]
    )
    executor._loop.session_manager.get_context_messages = AsyncMock(
        return_value=executor._loop.session_manager.get_messages_by_session.return_value
    )

    await _execute_command(executor, _confirm_inbound(approved=True))

    # 控制指令不产生 UserMsg
    saved = _saved_messages(executor)
    assert all(m.role != "user" for m in saved if isinstance(m, Msg))
    assert agent._captured_run_input.tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_confirm_result_injects_history_and_drives_resume():
    """恢复：读历史 context 注入新 agent，run() 收到 UserConfirmResultEvent。"""
    history = [
        Msg(role="user", content=[TextBlock(type="text", text="run ls")]),
        AssistantMsg(
            id="turn_orig",
            content=[
                ToolCallBlock(
                    id="call-1",
                    name="bash",
                    arguments={"command": "ls"},
                    state=ToolCallState.ASKING,
                )
            ],
        ),
    ]
    agent = ResumingAgent()
    executor = _make_executor(agent)
    _enable_builtin_commands(executor)
    executor._loop.session_manager.get_messages_by_session = AsyncMock(
        return_value=history
    )
    executor._loop.session_manager.get_context_messages = AsyncMock(
        return_value=history
    )

    await _execute_command(executor, _confirm_inbound(approved=True, tool_call_id="call-1"))

    # create_agent 收到注入了历史 context 的 state
    create_kwargs = executor._loop.agent_manager.create_agent.call_args.kwargs
    injected_state = create_kwargs["state"]
    assert injected_state is not None
    assert injected_state.context == history
    executor._loop.session_manager.get_context_messages.assert_awaited_once_with(
        "test-session"
    )
    assert executor._loop.session_manager.get_messages_by_session.await_count == 2
    checkpoint = executor._loop.session_manager.update_message.await_args.args[0]
    assert checkpoint.content[0].state == ToolCallState.ALLOWED

    # run() 的输入是 UserConfirmResultEvent，不是消息列表
    run_input = agent._captured_run_input
    assert isinstance(run_input, UserConfirmResultEvent)
    assert run_input.approved is True
    assert run_input.reply_id == "turn_orig"
    assert run_input.tool_call_id == "call-1"

    # runtime_context.reply_id 沿用原 reply_id，保证事件聚合回原 assistant Msg
    assert agent._captured_runtime_context["reply_id"] == "turn_orig"


@pytest.mark.asyncio
async def test_confirm_result_denied_still_resumes():
    """拒绝（approved=False）同样走恢复路径，run() 收到 approved=False 事件。"""
    agent = ResumingAgent()
    executor = _make_executor(agent)
    _enable_builtin_commands(executor)
    history = [
        AssistantMsg(
            id="turn_paused",
            content=[
                ToolCallBlock(
                    id="call-1",
                    name="bash",
                    arguments={},
                    state=ToolCallState.ASKING,
                )
            ],
        )
    ]
    executor._loop.session_manager.get_messages_by_session = AsyncMock(
        return_value=history
    )
    executor._loop.session_manager.get_context_messages = AsyncMock(
        return_value=history
    )

    await _execute_command(executor, _confirm_inbound(approved=False))

    run_input = agent._captured_run_input
    assert isinstance(run_input, UserConfirmResultEvent)
    assert run_input.approved is False


@pytest.mark.asyncio
async def test_confirm_resume_uses_structured_prompt_hook():
    agent = ResumingAgent()
    agent.system_prompt = "base"
    executor = _make_executor(agent)
    _enable_builtin_commands(executor)
    history = [
        AssistantMsg(
            id="turn_paused",
            content=[
                ToolCallBlock(
                    id="call-1",
                    name="bash",
                    arguments={},
                    state=ToolCallState.ASKING,
                )
            ],
        )
    ]
    executor._loop.session_manager.get_messages_by_session = AsyncMock(
        return_value=history
    )
    executor._loop.session_manager.get_context_messages = AsyncMock(
        return_value=history
    )
    executor._loop.hooks = HookRuntime(Context())
    executor._loop.agent_registry = AgentRegistry()
    executor._hooks = executor._loop.hooks
    executor._agent_registry = executor._loop.agent_registry

    async def inject(payload: PromptAssemblyPayload, next_):
        result = await next_()
        return type(result)(
            result.agent_id,
            result.session_id,
            result.workspace,
            result.contributions,
            result.text + "\nprivate tools ready",
        )

    executor._loop.hooks.register(
        SYSTEM_PROMPT_ASSEMBLE_SPEC,
        inject,
        owner="test-prompt",
        global_listener=True,
    )

    await _execute_command(executor, _confirm_inbound(approved=True))

    create_kwargs = executor._loop.agent_manager.create_agent.call_args.kwargs
    assert create_kwargs["config"].system_prompt.endswith("private tools ready")


@pytest.mark.asyncio
async def test_batch_confirm_checkpoints_all_before_resuming():
    """批量 /allow 先投影全部决定，再用最后一个事件触发一次恢复。"""
    history = [
        AssistantMsg(
            id="turn_paused",
            content=[
                ToolCallBlock(
                    id="call-1",
                    name="bash",
                    arguments={},
                    state=ToolCallState.ASKING,
                ),
                ToolCallBlock(
                    id="call-2",
                    name="read",
                    arguments={},
                    state=ToolCallState.ASKING,
                ),
            ],
        )
    ]
    agent = ResumingAgent()
    executor = _make_executor(agent)
    _enable_builtin_commands(executor)
    executor._loop.session_manager.get_messages_by_session = AsyncMock(
        return_value=history
    )
    executor._loop.session_manager.get_context_messages = AsyncMock(
        return_value=history
    )

    inbound = _confirm_inbound(tool_call_id="call-1 call-2")
    await _execute_command(executor, inbound)

    assert agent._captured_run_input.tool_call_id == "call-2"
    checkpoint = executor._loop.session_manager.update_message.await_args.args[0]
    assert [
        block.state for block in checkpoint.content if isinstance(block, ToolCallBlock)
    ] == [ToolCallState.ALLOWED, ToolCallState.ALLOWED]
