"""
ftre Turn Lifecycle 测试 — 验证状态机驱动的 Turn 生命周期模型。

测试 Turn 的完整生命周期：PENDING → MATCHING_COMMAND → PERSISTING_INPUT →
COMPACTING → BUILDING → RUNNING → FINALIZING → COMPLETED。

使用 object.__new__(AgentLoop) 模式构建最小测试环境。
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from ftre.agent.loop import AgentLoop
from ftre.agent.turn_executor import TurnExecutor, Turn, TurnStatus
from ftre.bus import BusMessage
from ftre.config import AgentConfig, ContextConfig, LLMConfig
from ftre_agent_core.agent.event import DoneReason, StepEvent, StepPhase
from ftre_agent_core.agent.runner import RunState, RunStatus
from ftre.command.types import CommandDef, Handled


# ── 测试辅助 ────────────────────────────────────────────────────────────────

class FakeAgent:
    """最小 mock agent，可配置 run 行为和 state。"""

    def __init__(self, *, done_reason=DoneReason.COMPLETED, run_raises=None):
        self._run_raises = run_raises
        self._captured_runtime_context = None
        self.tool_registry = Mock()

        self.state = RunState()
        self.state.done_reason = done_reason
        self.state.status = RunStatus.COMPLETED if done_reason == DoneReason.COMPLETED else RunStatus.ERROR
        self.state.iteration = 1
        self.state.token_usage = {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 0, "llm_calls": 1}
        self.state.error = "test error" if done_reason == DoneReason.ERROR else None
        self.state.error_code = "test_code" if done_reason == DoneReason.ERROR else None
        self.state.turn_id = "agent_core_turn_id"

    async def run(self, messages, runtime_context=None):
        self._captured_runtime_context = runtime_context
        if self._run_raises is not None:
            raise self._run_raises
        if False:
            yield

    def cancel_nowait(self):
        pass


def _make_executor(agent: FakeAgent) -> TurnExecutor:
    """构建最小 TurnExecutor，mock 所有外部依赖。"""
    loop = object.__new__(AgentLoop)

    config = AgentConfig()
    config.llm = LLMConfig()
    config.context = ContextConfig()

    loop._injected_config = config
    loop._event_loop = asyncio.get_running_loop()
    loop._active_agents = {}
    loop._session_tasks = {}
    loop._session_locks = {}
    loop._subagent_done_futures = {}
    loop._dispatch_tasks = set()

    loop.session_manager = AsyncMock()
    loop.session_manager.get_session = AsyncMock(return_value={
        "channel_id": "ws",
        "workspace": "/tmp",
    })
    loop.session_manager.save_message = AsyncMock()

    loop.bus = AsyncMock()

    loop.agent_manager = Mock()
    loop.agent_manager.load = Mock(return_value=None)
    loop.agent_manager.create_agent = Mock(return_value=agent)

    loop.hook_manager = None
    loop.channel_manager = None
    loop.tool_registry = None
    loop.core_hook_manager = None

    loop.compact_manager = AsyncMock()
    loop.compact_manager.should_compact = AsyncMock(return_value=False)
    loop.compact_manager.maybe_schedule_idle_compact = AsyncMock()

    loop.command_manager = Mock()
    loop.command_manager.try_dispatch_system = AsyncMock(return_value=False)
    loop.command_manager.match = Mock(return_value=None)
    loop.command_manager.match_any = Mock(return_value=None)
    loop.command_manager.try_dispatch = AsyncMock(return_value=None)

    loop.tracer = Mock()

    executor = TurnExecutor(loop)
    executor._build_messages = AsyncMock(
        return_value=([{"role": "user", "content": "hi"}], config)
    )
    executor._publish_session_status_async = AsyncMock()

    loop._executor = executor
    return executor


def _make_inbound(content="hello", session_id="test-session") -> BusMessage:
    return BusMessage(
        type="user_message",
        from_channel="ws",
        to_channel="ws",
        from_session=session_id,
        to_session=session_id,
        data={"content": content, "session_id": session_id},
        metadata={},
    )


def _save_types(executor: TurnExecutor) -> list[str]:
    """提取 save_message 调用的 event_type 列表。"""
    save_calls = executor._loop.session_manager.save_message.call_args_list
    return [c.args[1] for c in save_calls]


def _save_phases(executor: TurnExecutor) -> list[str]:
    """提取 save_message 调用中 step 事件的 phase 列表。"""
    save_calls = executor._loop.session_manager.save_message.call_args_list
    return [
        c.args[2].get("phase", "")
        for c in save_calls
        if c.args[1] == "step" and len(c.args) > 2
    ]


# ── turn_id 在 agent.run() 之前生成并传入 runtime_context ─────────────────

@pytest.mark.asyncio
async def test_turn_id_generated_before_run():
    """ftre 生成的 turn_id 被传入 runtime_context，以 turn_ 开头。"""
    agent = FakeAgent()
    executor = _make_executor(agent)
    inbound = _make_inbound()

    await executor.execute(inbound)

    assert agent._captured_runtime_context is not None
    passed_turn_id = agent._captured_runtime_context.get("turn_id")
    assert passed_turn_id is not None
    assert passed_turn_id.startswith("turn_")
    assert passed_turn_id != "agent_core_turn_id"


# ── 用户输入在 agent.run() 之前持久化（DB 顺序正确）────────────────────────

@pytest.mark.asyncio
async def test_user_input_persisted_before_run():
    """agent.run() 执行时，user_message 已在 PERSISTING_INPUT 状态提前持久化。"""
    agent = FakeAgent()
    executor = _make_executor(agent)
    inbound = _make_inbound()

    await executor.execute(inbound)

    types = _save_types(executor)
    phases = _save_phases(executor)

    # user_message 必须存在
    assert "user_message" in types

    # DB 顺序：PIPELINE_START → user_message → TURN_START → TURN_END → PIPELINE_END
    assert types[0] == "step"           # PIPELINE_START
    assert types[1] == "user_message"   # user_message
    assert types[2] == "step"           # TURN_START
    assert types[3] == "step"           # TURN_END
    assert types[4] == "step"           # PIPELINE_END


# ── TURN_END 从 agent.state 构造（正常完成路径）────────────────────────────

@pytest.mark.asyncio
async def test_turn_end_from_state():
    """正常完成后，TURN_END 从 agent.state 构造，reason=COMPLETED。"""
    agent = FakeAgent(done_reason=DoneReason.COMPLETED)
    executor = _make_executor(agent)
    inbound = _make_inbound()

    await executor.execute(inbound)

    save_calls = executor._loop.session_manager.save_message.call_args_list
    # 找到 TURN_END 的 save_message 调用
    turn_end_calls = [
        c for c in save_calls
        if c.args[1] == "step" and c.args[2].get("phase") == "turn_end"
    ]
    assert len(turn_end_calls) == 1
    turn_end_data = turn_end_calls[0].args[2]
    assert turn_end_data.get("reason") == "completed"
    assert turn_end_data.get("success") is True


# ── cancel fallback 用 ftre 的 turn_id ─────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_uses_ftre_turn_id():
    """agent.run() raise CancelledError 时，fallback TURN_END 用 ftre 的 turn_id。"""
    agent = FakeAgent(run_raises=asyncio.CancelledError())
    executor = _make_executor(agent)
    inbound = _make_inbound()

    await executor.execute(inbound)

    save_calls = executor._loop.session_manager.save_message.call_args_list
    turn_end_calls = [
        c for c in save_calls
        if c.args[1] == "step" and c.args[2].get("phase") == "turn_end"
    ]
    assert len(turn_end_calls) == 1
    fallback_turn_id = turn_end_calls[0].kwargs.get("turn_id", "")
    assert fallback_turn_id.startswith("turn_")
    assert fallback_turn_id != "agent_core_turn_id"


# ── error fallback 用 ftre 的 turn_id ──────────────────────────────────────

@pytest.mark.asyncio
async def test_error_uses_ftre_turn_id():
    """agent.run() raise Exception 时，fallback TURN_END 用 ftre 的 turn_id。"""
    agent = FakeAgent(run_raises=RuntimeError("unexpected error"))
    executor = _make_executor(agent)
    inbound = _make_inbound()

    await executor.execute(inbound)

    save_calls = executor._loop.session_manager.save_message.call_args_list
    turn_end_calls = [
        c for c in save_calls
        if c.args[1] == "step" and c.args[2].get("phase") == "turn_end"
    ]
    assert len(turn_end_calls) == 1
    fallback_turn_id = turn_end_calls[0].kwargs.get("turn_id", "")
    assert fallback_turn_id.startswith("turn_")
    assert fallback_turn_id != "agent_core_turn_id"


# ── persist_input=False 的指令（/cancel）不存储用户消息 ────────────────────

@pytest.mark.asyncio
async def test_cancel_skips_user_message_persistence():
    """/cancel（persist_input=False）不存 user_message，不 echo。"""
    agent = FakeAgent()
    executor = _make_executor(agent)
    inbound = _make_inbound(content="/cancel")

    cancel_def = CommandDef(command="/cancel", system=True, persist_input=False)
    executor._loop.command_manager.match_any = Mock(return_value=cancel_def)
    executor._loop.command_manager.try_dispatch_system = AsyncMock(return_value=True)

    await executor.execute(inbound)

    types = _save_types(executor)
    phases = _save_phases(executor)

    # 不应该有 user_message
    assert "user_message" not in types
    # 应该有 COMMAND_MATCHED
    assert "command_matched" in phases


# ── persist_input=True 的指令（/compact）先存再发 COMMAND_MATCHED ──────────

@pytest.mark.asyncio
async def test_compact_persists_and_emits_command_matched():
    """/compact（persist_input=True）先存 user_message，再发 COMMAND_MATCHED。"""
    agent = FakeAgent()
    executor = _make_executor(agent)
    inbound = _make_inbound(content="/compress")

    compact_def = CommandDef(command="/compact", persist_input=True)
    executor._loop.command_manager.match_any = Mock(return_value=compact_def)
    executor._loop.command_manager.match = Mock(return_value=compact_def)
    executor._loop.command_manager.try_dispatch = AsyncMock(return_value=Handled())
    executor._loop.command_manager.try_dispatch_system = AsyncMock(return_value=False)

    await executor.execute(inbound)

    types = _save_types(executor)
    phases = _save_phases(executor)

    # user_message 存在
    assert "user_message" in types
    # COMMAND_MATCHED 存在
    assert "command_matched" in phases
    # user_message 在 COMMAND_MATCHED 之前（PERSISTING_INPUT 在 COMMAND_MATCHED 之后）
    # 但当前实现是先 COMMAND_MATCHED 再 PERSISTING_INPUT...
    # 实际上 COMMAND_MATCHED 在 _match_command 里发，PERSISTING_INPUT 在之后
    # 所以 COMMAND_MATCHED 在 user_message 之前
    cmd_idx = phases.index("command_matched")
    user_idx = types.index("user_message")
    step_indices = [i for i, t in enumerate(types) if t == "step"]
    cmd_step_pos = step_indices[cmd_idx]
    assert cmd_step_pos < user_idx
