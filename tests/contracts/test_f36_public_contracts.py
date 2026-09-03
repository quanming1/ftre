"""F36.3 公共 Agent 契约门禁。"""

from __future__ import annotations

import asyncio

from ftre_agent import (
    LLM_ERROR_SPEC,
    Msg,
    ToolContext,
    ToolDefinition,
    ToolExecutionResult,
    ToolParameter,
    ToolView,
)
from ftre_agent.event import UserMessageEvent
from ftre_agent.message import UserMsg


def test_agent_contracts_are_core_free() -> None:
    assert Msg.__module__.startswith("ftre_agent.message")
    assert UserMsg.__module__.startswith("ftre_agent.message")
    assert UserMessageEvent.__module__.startswith("ftre_agent.event")
    assert LLM_ERROR_SPEC.name == "llm/error"


def test_tool_definition_is_declaration_with_execute_contract() -> None:
    definition = ToolDefinition(
        "echo",
        "echo input",
        [ToolParameter("value", "string", "value")],
        execute=lambda value: value,
    )
    assert definition.to_openai_dict()["function"]["name"] == "echo"
    assert definition.execute({"value": "ok"}) == "ok"
    assert definition.name == "echo"


def test_tool_view_is_runtime_port_without_registry_surface() -> None:
    assert hasattr(ToolView, "execute")
    assert "register" not in ToolView.__dict__
    result = ToolExecutionResult(output="ok")
    assert result.status == "completed"
    context = ToolContext("call-1", "echo", {"value": "ok"}, cancellation=asyncio.Event())
    assert context.call_id == "call-1"
