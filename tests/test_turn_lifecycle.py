"""
ftre Turn Lifecycle 测试 — 验证 ftre 侧的 turn_id 生成、TURN_START/TURN_END 构造、
用户输入持久化时机和 fallback turn_id 归属。

使用 object.__new__(AgentLoop) 模式构建最小测试环境（参考 test_subagent_done_future.py）。
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from ftre.agent.loop import AgentLoop
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

        # 预设 state
        self.state = RunState()
        self.state.done_reason = done_reason
        self.state.status = RunStatus.COMPLETED if done_reason == DoneReason.COMPLETED else RunStatus.ERROR
        self.state.iteration = 1
        self.state.token_usage = {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 0, "llm_calls": 1}
        self.state.error = "test error" if done_reason == DoneReason.ERROR else None
        self.state.error_code = "test_code" if done_reason == DoneReason.ERROR else None
        # turn_id 设为与 ftre 不同的值，以验证 fallback 不用它
        self.state.turn_id = "agent_core_turn_id"

    async def run(self, messages, runtime_context=None):
        self._captured_runtime_context = runtime_context
        if self._run_raises is not None:
            raise self._run_raises
        # 空 async generator（不 yield 任何事件）
        if False:
            yield

    def cancel_nowait(self):
        pass


def _make_loop(agent: FakeAgent) -> AgentLoop:
    """构建最小 AgentLoop，mock 所有外部依赖。"""
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

    # Mock pipeline: 直接调 _step_run
    from ftre.utils import Pipeline
    loop._pipeline = Pipeline("test")
    loop._pipeline.use(loop._step_compact, name="compact")
    loop._pipeline.use(loop._step_run, name="run")

    loop.tracer = Mock()

    # Mock _build_messages 和 _publish_session_status_async
    loop._build_messages = AsyncMock(return_value=([{"role": "user", "content": "hi"}], config))
    loop._publish_session_status_async = AsyncMock()

    return loop


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


# ── Step 16: turn_id 在 agent.run() 之前生成并传入 runtime_context ────────

@pytest.mark.asyncio
async def test_turn_id_generated_before_run():
    """ftre 生成的 turn_id 被传入 runtime_context，以 turn_ 开头。"""
    agent = FakeAgent()
    loop = _make_loop(agent)
    inbound = _make_inbound()

    from ftre.agent.loop import PipelineData
    data = PipelineData(inbound=inbound)
    data.turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    await loop._run_async(data)

    assert agent._captured_runtime_context is not None
    passed_turn_id = agent._captured_runtime_context.get("turn_id")
    assert passed_turn_id is not None
    assert passed_turn_id.startswith("turn_")
    # turn_id 应等于 data.turn_id（_dispatch 生成的）
    assert passed_turn_id == data.turn_id


# ── Step 17: TURN_START + 用户输入在 agent.run() 之前持久化 ───────────────

@pytest.mark.asyncio
async def test_user_input_persisted_before_run():
    """agent.run() 立即崩溃时，user_message 已在 _dispatch 中提前持久化。"""
    agent = FakeAgent(run_raises=RuntimeError("crash immediately"))
    loop = _make_loop(agent)
    inbound = _make_inbound()

    from ftre.agent.loop import PipelineData
    data = PipelineData(inbound=inbound)
    await loop._dispatch(data)

    save_calls = loop.session_manager.save_message.call_args_list
    # 顺序应为: PIPELINE_START(step), user_message, TURN_START(step), fallback TURN_END(step), PIPELINE_END(step)
    types = [c.args[1] for c in save_calls]
    
    # user_message 必须存在
    assert "user_message" in types
    
    # user_message 必须在第一个 TURN_START 之后的所有 step 之前（即 PIPELINE_START 之后、TURN_START 之前）
    user_msg_idx = types.index("user_message")
    # 至少有一个 step 在 user_message 之前（PIPELINE_START）
    assert user_msg_idx > 0
    assert types[0] == "step"  # PIPELINE_START

    # 验证 agent.run 被调用
    assert agent._captured_runtime_context is not None


# ── Step 18: TURN_END 从 agent.state 构造（正常完成路径）──────────────────

@pytest.mark.asyncio
async def test_turn_end_from_state():
    """正常完成后，TURN_END 从 agent.state 构造，reason=COMPLETED。"""
    agent = FakeAgent(done_reason=DoneReason.COMPLETED)
    loop = _make_loop(agent)
    inbound = _make_inbound()

    from ftre.agent.loop import PipelineData
    data = PipelineData(inbound=inbound)
    data.turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    await loop._run_async(data)

    # 最后一次 save_message 应该是 TURN_END (event_type="step")
    save_calls = loop.session_manager.save_message.call_args_list
    last_call = save_calls[-1]
    assert last_call.args[1] == "step"

    # 验证 turn_end 的 turn_id 与 ftre 生成的一致（非 agent.state.turn_id）
    turn_end_data = last_call.kwargs.get("turn_id", "")
    assert turn_end_data != "agent_core_turn_id"
    assert turn_end_data.startswith("turn_")
    assert turn_end_data == data.turn_id


# ── Step 19a: cancel fallback 用 ftre 的 turn_id ──────────────────────────

@pytest.mark.asyncio
async def test_cancel_uses_ftre_turn_id():
    """agent.run() raise CancelledError 时，fallback TURN_END 用 ftre 的 turn_id。"""
    agent = FakeAgent(run_raises=asyncio.CancelledError())
    loop = _make_loop(agent)
    inbound = _make_inbound()

    from ftre.agent.loop import PipelineData
    data = PipelineData(inbound=inbound)
    data.turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    await loop._run_async(data)

    # 最后一次 save_message 应该是 fallback TURN_END
    save_calls = loop.session_manager.save_message.call_args_list
    last_call = save_calls[-1]
    assert last_call.args[1] == "step"
    fallback_turn_id = last_call.kwargs.get("turn_id", "")
    assert fallback_turn_id.startswith("turn_")
    assert fallback_turn_id != "agent_core_turn_id"
    assert fallback_turn_id == data.turn_id


# ── Step 19b: error fallback 用 ftre 的 turn_id ───────────────────────────

@pytest.mark.asyncio
async def test_error_uses_ftre_turn_id():
    """agent.run() raise Exception 时，fallback TURN_END 用 ftre 的 turn_id。"""
    agent = FakeAgent(run_raises=RuntimeError("unexpected error"))
    loop = _make_loop(agent)
    inbound = _make_inbound()

    from ftre.agent.loop import PipelineData
    data = PipelineData(inbound=inbound)
    data.turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    await loop._run_async(data)

    # 最后一次 save_message 应该是 fallback TURN_END
    save_calls = loop.session_manager.save_message.call_args_list
    last_call = save_calls[-1]
    assert last_call.args[1] == "step"
    fallback_turn_id = last_call.kwargs.get("turn_id", "")
    assert fallback_turn_id.startswith("turn_")
    assert fallback_turn_id != "agent_core_turn_id"


# ── persist_input=False 的指令（/cancel）不存储用户消息 ──────────────────

@pytest.mark.asyncio
async def test_cancel_skips_user_message_persistence():
    """/cancel（persist_input=False）不存 user_message，不 echo。"""
    agent = FakeAgent()
    loop = _make_loop(agent)
    inbound = _make_inbound(content="/cancel")

    cancel_def = CommandDef(command="/cancel", system=True, persist_input=False)
    loop.command_manager.match_any = Mock(return_value=cancel_def)
    loop.command_manager.try_dispatch_system = AsyncMock(return_value=True)

    from ftre.agent.loop import PipelineData
    data = PipelineData(inbound=inbound)
    await loop._dispatch(data)

    save_calls = loop.session_manager.save_message.call_args_list
    types = [c.args[1] for c in save_calls]
    # 不应该有 user_message
    assert "user_message" not in types
    # 应该有 COMMAND_MATCHED（step 类型，phase=command_matched）
    phases = [
        c.args[2].get("phase", "")
        for c in save_calls
        if c.args[1] == "step" and len(c.args) > 2
    ]
    assert "command_matched" in phases


# ── persist_input=True 的指令（/compact）存储用户消息 + 发 COMMAND_MATCHED ──

@pytest.mark.asyncio
async def test_compact_persists_and_emits_command_matched():
    """/compact（persist_input=True）先存 user_message，再发 COMMAND_MATCHED。"""
    agent = FakeAgent()
    loop = _make_loop(agent)
    inbound = _make_inbound(content="/compress")

    compact_def = CommandDef(command="/compact", persist_input=True)
    loop.command_manager.match_any = Mock(return_value=compact_def)
    loop.command_manager.match = Mock(return_value=compact_def)
    loop.command_manager.try_dispatch = AsyncMock(return_value=Handled())
    loop.command_manager.try_dispatch_system = AsyncMock(return_value=False)

    from ftre.agent.loop import PipelineData
    data = PipelineData(inbound=inbound)
    await loop._dispatch(data)

    save_calls = loop.session_manager.save_message.call_args_list
    types = [c.args[1] for c in save_calls]

    # user_message 存在
    assert "user_message" in types

    # user_message 在 COMMAND_MATCHED 之前（先存再发 COMMAND_MATCHED）
    user_idx = types.index("user_message")
    phases = [
        c.args[2].get("phase", "")
        for c in save_calls
        if c.args[1] == "step" and len(c.args) > 2
    ]
    assert "command_matched" in phases
    # user_message 的全局索引应小于 command_matched 所在 step 的全局索引
    cmd_phase_idx = phases.index("command_matched")
    step_indices = [i for i, t in enumerate(types) if t == "step"]
    assert user_idx < step_indices[cmd_phase_idx]
