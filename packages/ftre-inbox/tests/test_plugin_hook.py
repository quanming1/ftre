from __future__ import annotations

import asyncio

import pytest
from cordis import Context
from ftre_agent_core.agent import ReActAgent
from ftre_agent_core.hooks import AGENT_BEFORE_REASONING_SPEC, BeforeReasoningPayload
from ftre_agent_core.llm import (
    BlockEnd,
    BlockStart,
    FinishChunk,
    FinishReason,
    TextDeltaChunk,
)
from ftre_agent_core.tool import Tool
from ftre_inbox.plugin import apply
from ftre_inbox.protocol import InboundMessage

from ftre.platform.hooks import HookRuntime
from ftre.services.agent.registry import AgentRegistry


class BusyAgent:
    def is_busy(self, _session_id: str) -> bool:
        return True


class SequenceLLM:
    model = "fake"

    def __init__(self, sequences):
        self.sequences = sequences
        self.calls = []

    async def stream(self, messages, tools=None):
        self.calls.append(messages)
        for chunk in self.sequences[min(len(self.calls) - 1, len(self.sequences) - 1)]:
            yield chunk


def _tool_sequence():
    return [
        BlockStart(index=0, block_type="tool-call"),
        BlockEnd(index=0, block={
            "type": "tool-call",
            "id": "call-1",
            "name": "pause",
            "arguments": "{}",
        }),
        FinishChunk(reason=FinishReason(kind="tool-calls")),
    ]


def _text_sequence(text: str):
    return [
        BlockStart(index=0, block_type="text"),
        TextDeltaChunk(index=0, text=text),
        BlockEnd(index=0, block={"type": "text", "text": text}),
        FinishChunk(reason=FinishReason(kind="stop")),
    ]


@pytest.mark.asyncio
async def test_plugin_consumes_next_step_through_core_hook(tmp_path):
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("sessions", None)
    context.provide("agents", BusyAgent())

    await apply(context, {"inbox_dir": str(tmp_path)})
    inbox = context.get("inbox", strict=False)
    await inbox.steer(InboundMessage("s1", "steer-1", "ws", "请改用中文"))

    registry = AgentRegistry()
    registry.ensure("default")
    scope = runtime.context_for_scope(registry.scope_carrier("default"))
    result = await runtime.dispatch(
        AGENT_BEFORE_REASONING_SPEC,
        BeforeReasoningPayload(
            agent=object(),
            session_id="s1",
            turn_id="turn-1",
            iteration=2,
            cancellation=asyncio.Event(),
        ),
        context=scope,
    )

    assert [message["content"] for message in result.messages] == ["请改用中文"]
    assert not (await inbox.snapshot("s1")).has_pending
    cleanup = context.dispose()
    if cleanup is not None:
        await cleanup


@pytest.mark.asyncio
async def test_running_core_turn_consumes_steer_before_next_reasoning(tmp_path):
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("sessions", None)
    context.provide("agents", BusyAgent())
    await apply(context, {"inbox_dir": str(tmp_path)})
    inbox = context.get("inbox", strict=False)
    registry = AgentRegistry()
    registry.ensure("default")
    scope = runtime.context_for_scope(registry.scope_carrier("default"))

    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def pause_tool():
        tool_started.set()
        await release_tool.wait()
        return "tool complete"

    agent = ReActAgent(
        model="fake", api_key="fake", hooks=runtime, max_iterations=3,
        hook_context=scope,
        tool_registry=None,
    )
    agent.tool_registry.register(Tool(name="pause", func=pause_tool))
    llm = SequenceLLM([_tool_sequence(), _text_sequence("steer 已消费")])
    agent.runner._llm = llm

    stream = agent.run(
        "开始",
        runtime_context={
            "session_id": "s1",
            "agent_id": "default",
            "agent_subject": registry.ensure("default"),
            "cancellation": asyncio.Event(),
        },
    )
    # 先消费 ReplyStart，再让另一项任务继续驱动真实 ReAct 循环。
    await stream.__anext__()
    stream_task = asyncio.create_task(_consume_remaining(stream))
    await asyncio.wait_for(tool_started.wait(), timeout=1)
    await inbox.steer(InboundMessage("s1", "steer-1", "ws", "请改用中文"))
    release_tool.set()
    await asyncio.wait_for(stream_task, timeout=2)

    assert len(llm.calls) == 2
    assert any("请改用中文" in str(message) for message in llm.calls[1])
    assert not (await inbox.snapshot("s1")).has_pending
    cleanup = context.dispose()
    if cleanup is not None:
        await cleanup


async def _consume_remaining(stream):
    """独立消费 generator 的剩余事件，便于测试在 Tool 阻塞时插入 steer。"""
    async for _event in stream:
        pass
