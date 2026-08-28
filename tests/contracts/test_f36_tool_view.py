"""F36.4 ToolService/ToolView 边界与执行流水线门禁。"""

from __future__ import annotations

import asyncio

from ftre_agent.tool import Injected, ToolContext, ToolDefinition

from ftre.services.tools import ToolService

WORKSPACE = Injected("workspace")


def test_prepare_view_uses_snapshot_and_scoped_shadow() -> None:
    service = ToolService()
    service.register(ToolDefinition("echo", execute=lambda: "global"), owner="base")
    service.register(
        ToolDefinition("echo", execute=lambda: "scoped"),
        owner="agent",
        scope="agent:a",
    )

    view = asyncio.run(service.prepare_view("a", "session"))
    assert view.names == ("echo",)
    assert asyncio.run(
        view.execute("echo", {}, ToolContext("c", "echo", {}, {"agent_id": "a"}))
    ).output == "scoped"


def test_view_execute_normalizes_result_and_injected_context() -> None:
    service = ToolService()
    service.register(
        ToolDefinition("read", execute=lambda path, workspace=WORKSPACE: f"{workspace}:{path}"),
        owner="test",
    )
    view = asyncio.run(service.prepare_view("a", "session"))
    result = asyncio.run(
        view.execute(
            "read",
            {"path": "a.txt"},
            ToolContext(
                "c", "read", {}, {"workspace": "ws", "session_id": "session"}
            ),
        )
    )
    assert result.status == "completed"
    assert result.output == "ws:a.txt"
