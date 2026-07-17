"""
ftre Turn Lifecycle 测试 — 验证 ftre 侧的 turn_id 生成、TURN_START/TURN_END 构造、
用户输入持久化时机和 fallback turn_id 归属。

使用 object.__new__(AgentLoop) 模式构建最小测试环境（参考 test_subagent_done_future.py）。
"""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from ftre.agent.loop import AgentLoop
from ftre.bus import BusMessage
from ftre.config import AgentConfig, ContextConfig, LLMConfig
from ftre_agent_core.agent.event import DoneReason, StepEvent, StepPhase
from ftre_agent_core.agent.runner import RunState, RunStatus


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
    loop._subagent_done_futures = {}

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

    await loop._run_async(inbound, need_compact=False)

    assert agent._captured_runtime_context is not None
    passed_turn_id = agent._captured_runtime_context.get("turn_id")
    assert passed_turn_id is not None
    assert passed_turn_id.startswith("turn_")


# ── Step 17: TURN_START + 用户输入在 agent.run() 之前持久化 ───────────────

@pytest.mark.asyncio
async def test_user_input_persisted_before_run():
    """agent.run() 立即崩溃时，TURN_START + user_message 已在 run 之前持久化。"""
    agent = FakeAgent(run_raises=RuntimeError("crash immediately"))
    loop = _make_loop(agent)
    inbound = _make_inbound()

    await loop._run_async(inbound, need_compact=False)

    # session_manager.save_message 应被调用至少 2 次：TURN_START + user_message
    save_calls = loop.session_manager.save_message.call_args_list
    assert len(save_calls) >= 2

    # 第一次应该是 TURN_START (event_type="step")
    first_call_args = save_calls[0]
    assert first_call_args.args[1] == "step"

    # 第二次应该是 user_message
    second_call_args = save_calls[1]
    assert second_call_args.args[1] == "user_message"

    # 两次都应在 agent.run() 之前 — 验证 agent._captured_runtime_context 被设置
    # 说明 run 被调用了（但在 save 之后）
    assert agent._captured_runtime_context is not None


# ── Step 18: TURN_END 从 agent.state 构造（正常完成路径）──────────────────

@pytest.mark.asyncio
async def test_turn_end_from_state():
    """正常完成后，TURN_END 从 agent.state 构造，reason=COMPLETED。"""
    agent = FakeAgent(done_reason=DoneReason.COMPLETED)
    loop = _make_loop(agent)
    inbound = _make_inbound()

    await loop._run_async(inbound, need_compact=False)

    # 最后一次 save_message 应该是 TURN_END (event_type="step")
    save_calls = loop.session_manager.save_message.call_args_list
    last_call = save_calls[-1]
    assert last_call.args[1] == "step"

    # 验证 turn_end 的 turn_id 与 ftre 生成的一致（非 agent.state.turn_id）
    turn_end_data = last_call.kwargs.get("turn_id", "")
    assert turn_end_data != "agent_core_turn_id"
    assert turn_end_data.startswith("turn_")


# ── Step 19a: cancel fallback 用 ftre 的 turn_id ──────────────────────────

@pytest.mark.asyncio
async def test_cancel_uses_ftre_turn_id():
    """agent.run() raise CancelledError 时，fallback TURN_END 用 ftre 的 turn_id。"""
    agent = FakeAgent(run_raises=asyncio.CancelledError())
    loop = _make_loop(agent)
    inbound = _make_inbound()

    await loop._run_async(inbound, need_compact=False)

    # 最后一次 save_message 应该是 fallback TURN_END
    save_calls = loop.session_manager.save_message.call_args_list
    last_call = save_calls[-1]
    assert last_call.args[1] == "step"
    fallback_turn_id = last_call.kwargs.get("turn_id", "")
    assert fallback_turn_id.startswith("turn_")
    assert fallback_turn_id != "agent_core_turn_id"


# ── Step 19b: error fallback 用 ftre 的 turn_id ───────────────────────────

@pytest.mark.asyncio
async def test_error_uses_ftre_turn_id():
    """agent.run() raise Exception 时，fallback TURN_END 用 ftre 的 turn_id。"""
    agent = FakeAgent(run_raises=RuntimeError("unexpected error"))
    loop = _make_loop(agent)
    inbound = _make_inbound()

    await loop._run_async(inbound, need_compact=False)

    # 最后一次 save_message 应该是 fallback TURN_END
    save_calls = loop.session_manager.save_message.call_args_list
    last_call = save_calls[-1]
    assert last_call.args[1] == "step"
    fallback_turn_id = last_call.kwargs.get("turn_id", "")
    assert fallback_turn_id.startswith("turn_")
    assert fallback_turn_id != "agent_core_turn_id"
