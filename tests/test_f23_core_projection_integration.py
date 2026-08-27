"""F23 跨 Core/SessionProjection 的 A→User→B 协议回归。"""

import pytest
from ftre_agent_core.agent import ReActAgent
from ftre_agent_core.event import UserMessageEvent
from ftre_agent_core.tool import Tool
from ftre_llm.events import (
    BlockEnd,
    BlockStart,
    FinishChunk,
    FinishReason,
    TextDeltaChunk,
    ToolCallDeltaChunk,
)

from ftre.services.session import SessionService


class TwoStepProvider:
    """不访问网络的 Core provider：第一步 Tool，第二步文本。"""

    model = "fake-f23"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, _messages, tools=None):
        del tools
        self.calls += 1
        if self.calls == 1:
            yield BlockStart(index=0, block_type="tool-call")
            yield ToolCallDeltaChunk(
                index=0,
                call_id="call-f23",
                name="echo",
                arguments_delta='{"value":"ok"}',
            )
            yield BlockEnd(
                index=0,
                block={
                    "type": "tool-call",
                    "id": "call-f23",
                    "name": "echo",
                    "arguments": '{"value":"ok"}',
                },
            )
            yield FinishChunk(reason=FinishReason(kind="tool-calls"))
            return
        yield BlockStart(index=0, block_type="text")
        yield TextDeltaChunk(index=0, text="完成")
        yield BlockEnd(index=0, block={"type": "text", "text": "完成"})
        yield FinishChunk(reason=FinishReason(kind="stop"))

    def cancel(self) -> None:
        return None


class SteeringHook:
    async def dispatch(self, spec, payload, *, context=None):
        del context
        if spec.name == "agent/before-reasoning" and payload.iteration == 2:
            from ftre_agent_core.hooks import BeforeReasoningResult

            return BeforeReasoningResult(({
                "id": "user-f23",
                "role": "user",
                "content": "请继续",
                "metadata": {"request_id": "request-f23"},
            },))
        return await spec.default(payload)


@pytest.mark.asyncio
async def test_core_events_and_session_projection_share_message_id_boundary(tmp_path):
    agent = ReActAgent(
        model="fake-f23",
        api_key="fake",
        hooks=SteeringHook(),
        max_iterations=3,
    )
    agent.tool_registry.register(Tool(name="echo", func=lambda value: value))
    agent.runner.set_llm(TwoStepProvider())
    events = [event async for event in agent.run("开始")]

    model_message_ids = [
        event.message_id
        for event in events
        if event.type == "MODEL_CALL_START"
    ]
    assert len(model_message_ids) == 2
    assert model_message_ids[0] != model_message_ids[1]
    assert len({event.reply_id for event in events if getattr(event, "reply_id", None)}) == 1

    sessions = SessionService(sessions_dir=str(tmp_path / "sessions"))
    await sessions.init()
    session_id = await sessions.create_session("ws")
    projection = sessions.projection
    second_message_id = model_message_ids[1]
    for event in events:
        if event.type == "MODEL_CALL_START" and event.message_id == second_message_id:
            await projection.apply(session_id, UserMessageEvent(
                id="user-f23",
                reply_id="input-f23",
                data={"request_id": "request-f23", "content": "请继续"},
                content=[{"type": "text", "text": "请继续"}],
                message_metadata={"request_id": "request-f23"},
            ))
        await projection.apply(session_id, event)

    messages = await sessions.get_messages_by_session(session_id)
    assert [message["role"] for message in messages] == [
        "assistant", "user", "assistant",
    ]
    assert [message["id"] for message in messages] == [
        model_message_ids[0], "user-f23", model_message_ids[1],
    ]
    await sessions.close()
