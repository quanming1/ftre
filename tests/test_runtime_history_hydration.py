from types import SimpleNamespace

import pytest
from ftre_agent.message import TextBlock
from ftre_agent_runtime.message_context import MessageContext
from ftre_agent_runtime.react_agent import ReActAgent


@pytest.mark.asyncio
async def test_rehydrated_assistant_history_is_not_emitted_as_new_snapshot():
    async def stream(*_args, **_kwargs):
        if False:
            yield None

    agent = ReActAgent(
        model="test-model",
        api_key="test-key",
        tool_view=SimpleNamespace(to_openai_tools=list),
        llm=SimpleNamespace(stream=stream, cancel=lambda: None),
    )

    agent.runner._prepare_new_reply(
        [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ],
        {},
    )

    MessageContext.append_reply_blocks(
        agent.state.context,
        agent.runner.state.message_id,
        [TextBlock(text="current answer")],
    )

    events = agent.build_final_assistant_events()

    assert len(events) == 1
    assert events[0].message_id == agent.runner.state.message_id
    assert events[0].data.message["content"][0]["text"] == "current answer"
