from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from ftre_agent_core.event import HintBlockEvent
from ftre_agent_core.tool import Tool, ToolRegistry

from cordis import Context, FiberState
from ftre.services.agent.config import AgentConfig
from ftre.services.agent.runtime.hooks import BEFORE_AGENT_RUN, AgentRunContext
from ftre.services.http.service import HttpService
from ftre.services.tools import ToolService
from ftre.services.tools.builtin import build_default_tools
from ftre.services.tools.builtin._workspace import WorkspaceAccessor
from ftre.services.tools.builtin.read import create_read_tool


def _dummy_tool(name: str = "dummy") -> Tool:
    def dummy() -> str:
        return "ok"

    return Tool(name=name, description="dummy tool", parameters=[], func=dummy)


def test_tool_registry_overwrites_duplicate_names():
    registry = ToolRegistry()
    registry.register(_dummy_tool("dup"))
    registry.register(_dummy_tool("dup"))
    assert len(registry) == 1
    assert registry.get("dup") is not None


def test_build_default_tools_includes_registry_tools():
    registry = ToolRegistry()
    registry.register(_dummy_tool("extra"))
    assert "extra" in build_default_tools(tool_registry=registry).names


def test_read_tool_reads_relative_image_path(tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
        b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    result = create_read_tool().func(
        "screen.png",
        ws=_FakeWorkspace(str(tmp_path)),
        llm_config=SimpleNamespace(vision=True),
    )
    assert isinstance(result, HintBlockEvent)
    assert result.metadata["path"] == str(image.resolve())


@pytest.mark.asyncio
async def test_cordis_plugin_failure_rolls_back_registered_tools():
    root = Context()
    tools = ToolService(ToolRegistry())
    root.provide("tools", tools)

    class FailingPlugin:
        inject = ("tools",)
        provide = ()

        def apply(self, ctx):
            ctx.effect(ctx.tools.register(_dummy_tool("leaked"), owner="failing"))
            raise RuntimeError("boom")

    fiber = root.plugin(FailingPlugin, id="failing")
    await root.settle()
    assert fiber.state is FiberState.FAILED
    assert tools.snapshot() == ()
    await root.dispose()


@pytest.mark.asyncio
async def test_cordis_plugin_contributions_and_router_are_reversible():
    root = Context()
    tools = ToolService(ToolRegistry())
    http = HttpService()
    root.provide("tools", tools)
    root.provide("http", http)

    class Plugin:
        inject = ("tools", "http")
        provide = ()

        def apply(self, ctx):
            ctx.effect(ctx.tools.register(_dummy_tool("from_plugin"), owner="plugin"))
            router = APIRouter()

            @router.get("/ping")
            def ping():
                return {"pong": True}

            ctx.effect(ctx.http.register_router(router, owner="plugin"))

    fiber = root.plugin(Plugin, id="plugin")
    await root.settle()
    assert fiber.state is FiberState.ACTIVE
    assert tools.snapshot()[0].name == "from_plugin"
    assert any(item["path"] == "/api/ping" for item in http.snapshot())
    await root.dispose()
    assert tools.snapshot() == ()
    assert http.snapshot() == ()


@pytest.mark.asyncio
async def test_cordis_event_filter_can_insert_messages():
    root = Context()

    def rewrite(ctx):
        ctx.messages.insert(0, {"role": "system", "content": "persona: Alice"})
        return ctx

    root.on(BEFORE_AGENT_RUN, rewrite)
    ctx = AgentRunContext(
        session_id="sess_1",
        channel_id="ws",
        messages=[{"role": "system", "content": "base"}],
        config=AgentConfig(),
    )
    ctx = await root.filter(BEFORE_AGENT_RUN, ctx)
    assert [message["content"] for message in ctx.messages] == ["persona: Alice", "base"]


class _FakeWorkspace(WorkspaceAccessor):
    def __init__(self, cwd: str):
        self.cwd = cwd

    def get(self) -> str:
        return self.cwd
