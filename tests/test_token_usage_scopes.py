from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ftre_agent.message import AssistantMsg
from ftre_agent.message._msg import MsgToken, TokenUsage
from ftre_agent.session import derive_messages
from ftre_agent.types import ReplyFinishedReason
from ftre_agent_runtime.react_runner import ReActRunner
from ftre_agent_runtime.run_state import RunState
from ftre_compaction.service import CompactionService

from ftre.services.session.service import _compute_token_usage


def _usage(prompt: int, completion: int, total: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def test_turn_end_usage_does_not_replace_last_call_usage():
    assistant = AssistantMsg(
        id="m1",
        content="reply",
        token=MsgToken(
            usage=TokenUsage(**_usage(120, 20, 140)),
            last_call_usage=TokenUsage(**_usage(42_000, 977, 42_977)),
        ),
    )
    events = [
        {
            "type": "assistant/message",
            "seq": 0,
            "time": 1,
            "message_id": "m1",
            "data": {"message": assistant.model_dump(mode="json")},
        },
        {
            "type": "turn/end",
            "seq": 1,
            "time": 2,
            "message_id": "m1",
            "data": {
                "turn_id": "t1",
                "request_id": "r1",
                "outcome": "completed",
                "reason": "completed",
                "usage": _usage(772_206, 7_041, 779_247),
            },
        },
    ]

    result = derive_messages(events)[0]

    assert result.token is not None
    assert result.token.usage.total_tokens == 779_247
    assert result.token.last_call_usage.total_tokens == 42_977


def test_context_usage_uses_last_prompt_not_turn_total():
    assistant = AssistantMsg(
        id="m1",
        content="reply",
        token=MsgToken(
            usage=TokenUsage(**_usage(772_206, 7_041, 779_247)),
            last_call_usage=TokenUsage(**_usage(42_000, 977, 42_977)),
        ),
    )
    usage = _compute_token_usage("s1", [assistant.model_dump(mode="json")])

    assert usage["total"] == 42_977
    assert usage["context_tokens"] == 42_000


def test_context_usage_does_not_count_completion_when_prompt_is_missing():
    assistant = AssistantMsg(
        id="m1",
        content="reply",
        token=MsgToken(
            usage=TokenUsage(**_usage(0, 7_041, 779_247)),
            last_call_usage=TokenUsage(**_usage(0, 7_041, 779_247)),
        ),
    )
    usage = _compute_token_usage("s1", [assistant.model_dump(mode="json")])

    assert usage["context_tokens"] == 772_206


def test_runtime_builds_one_final_assistant_snapshot_with_both_scopes():
    class AgentState:
        def __init__(self):
            self.context = [AssistantMsg(id="m1", content="reply")]

    class Agent:
        model = "glm-5.3"

        def __init__(self):
            self.state = AgentState()

    runner = object.__new__(ReActRunner)
    runner.agent = Agent()
    runner.state = RunState(message_id="m1")
    runner.state.token_usage = _usage(772_206, 7_041, 779_247)
    runner.state.last_call_usage = _usage(42_000, 977, 42_977)

    event = runner.build_final_assistant_event()
    payload = event.data.message

    assert event.type == "assistant/message"
    assert payload["token"]["usage"]["total_tokens"] == 779_247
    assert payload["token"]["last_call_usage"]["total_tokens"] == 42_977


def test_runtime_emits_each_changed_assistant_snapshot_once():
    old = AssistantMsg(id="old", content="first part")
    current = AssistantMsg(id="current", content="second part")

    class AgentState:
        def __init__(self):
            self.context = [old, current]

    class Agent:
        model = "glm-5.3"

        def __init__(self):
            self.state = AgentState()

    runner = object.__new__(ReActRunner)
    runner.agent = Agent()
    runner.state = RunState(message_id="current", done_reason=ReplyFinishedReason.COMPLETED)
    runner._assistant_baseline = {
        "old": json.dumps(old.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
    }

    events = runner.build_final_assistant_events()
    assert [event.message_id for event in events] == ["current"]
    assert events[0].data.message["finished_at"]
    assert runner.build_final_assistant_events() == []


@pytest.mark.asyncio
async def test_compaction_uses_current_context_not_turn_total():
    sessions = SimpleNamespace(
        get_token_usage=AsyncMock(
            return_value={
                "total": 779_247,
                "context_tokens": 42_000,
                "pending_estimated": 0,
            }
        )
    )
    service = CompactionService(session_manager=sessions)
    config = SimpleNamespace(
        llm=SimpleNamespace(context_window=1_000_000, max_output=131_072)
    )

    assert not await service.should_compact("s1", "ws", config)

    sessions.get_token_usage.return_value = {
        "total": 42_000,
        "context_tokens": 700_000,
        "pending_estimated": 0,
    }
    assert await service.should_compact("s1", "ws", config)
