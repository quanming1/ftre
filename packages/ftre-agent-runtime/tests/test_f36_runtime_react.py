"""F36.5 Runtime ReAct 状态机回归。"""

from __future__ import annotations

import asyncio

from ftre_agent import AgentConfig, AgentCreateSpec
from ftre_agent.event import ReplyEndEvent, TextBlockDeltaEvent
from ftre_agent.tool import ToolContext, ToolExecutionResult
from ftre_agent_runtime import ReActAgent
from ftre_agent_runtime.runtime_factory import AgentLoopHandle
from ftre_llm import BlockEnd, BlockStart, FinishChunk, FinishReason, TextDeltaChunk


class FakeLlm:
    async def stream(self, _messages, _tools=None):
        yield BlockStart(index=0, block_type="text")
        yield TextDeltaChunk(index=0, text="hello")
        yield BlockEnd(index=0, block={"type": "text", "text": "hello"})
        yield FinishChunk(reason=FinishReason(kind="stop"))

    def cancel(self) -> None:
        return None


class EmptyView:
    names = ()

    def to_openai_tools(self):
        return []

    async def execute(
        self, _name: str, _arguments: dict, _context: ToolContext
    ) -> ToolExecutionResult:
        return ToolExecutionResult(status="failed", error="unknown tool")


def test_runtime_react_agent_uses_public_contracts_and_emits_real_events() -> None:
    agent = ReActAgent(
        model="fake",
        api_key="test",
        tool_view=EmptyView(),
        llm=FakeLlm(),
    )
    events = asyncio.run(_collect(agent))
    assert any(isinstance(event, TextBlockDeltaEvent) for event in events)
    assert any(isinstance(event, ReplyEndEvent) for event in events)


async def _collect(agent: ReActAgent):
    return [event async for event in agent.run("hello")]


def test_runtime_handle_stream_wraps_real_events() -> None:
    class Loop:
        async def stream_input(self, _inbound):
            yield TextBlockDeltaEvent(reply_id="reply", block_id="block", delta="text")

    spec = AgentCreateSpec("agent", AgentConfig(), session_id="session")
    handle = AgentLoopHandle(type("Factory", (), {"_loop": Loop(), "_to_inbound": staticmethod(lambda request: request)})(), spec)
    envelopes = asyncio.run(_collect_stream(handle))
    assert envelopes[0].event.delta == "text"
    assert envelopes[0].sequence == 0


async def _collect_stream(handle):
    request = type("Request", (), {"request_id": "r"})()
    return [event async for event in handle.stream(request)]
