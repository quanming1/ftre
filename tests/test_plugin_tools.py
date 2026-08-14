from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from ftre_agent_core.event import HintBlockEvent
from ftre_agent_core.tool import Tool, ToolRegistry

from ftre.config import AgentConfig
from ftre.plugin import (
    BEFORE_AGENT_RUN,
    AgentRunContext,
    EventHub,
    FtreContext,
    Plugin,
    PluginRegistry,
)
from ftre.tools import build_default_tools
from ftre.tools._workspace import WorkspaceAccessor
from ftre.tools.read import create_read_tool


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


def test_build_default_tools_omits_see_img_without_vision():
    assert (
        "see_img"
        not in build_default_tools(llm_config=SimpleNamespace(vision=False)).names
    )


def test_build_default_tools_omits_see_img_with_vision():
    assert (
        "see_img"
        not in build_default_tools(llm_config=SimpleNamespace(vision=True)).names
    )


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
    assert result.metadata["hide"] is True
    assert result.metadata["path"] == str(image.resolve())
    assert "data:image" in result.hint


def test_read_tool_rejects_image_without_vision(tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(b"not decoded because vision is disabled")
    result = create_read_tool().func(
        "screen.png",
        ws=_FakeWorkspace(str(tmp_path)),
        llm_config=SimpleNamespace(vision=False),
    )
    assert "当前模型不支持视觉输入" in result


@pytest.mark.asyncio
async def test_plugin_setup_failure_rolls_back_registered_tools():
    class FailingToolPlugin(Plugin):
        name = "failing_tool"
        inject = ("tool_registry",)

        async def setup(self, ctx, config):
            ctx.tool_registry.register(_dummy_tool("leaked"))
            raise RuntimeError("boom")

    _root, registry, tools = _plugin_runtime()
    instance = await registry.register(FailingToolPlugin)
    assert instance.error is not None
    assert tools.get("leaked") is None


@pytest.mark.asyncio
async def test_plugin_context_registers_and_cleans_shared_tool():
    class ToolPlugin(Plugin):
        name = "tool_plugin"
        inject = ("tool_registry",)

        async def setup(self, ctx, config):
            ctx.tool_registry.register(_dummy_tool("from_plugin"))

    _root, registry, tools = _plugin_runtime()
    await registry.register(ToolPlugin)
    assert [tool.name for tool in tools.snapshot()] == ["from_plugin"]
    await registry.unload("tool_plugin")
    assert tools.snapshot() == []


@pytest.mark.asyncio
async def test_plugin_context_registers_router():
    class RouterPlugin(Plugin):
        name = "router_plugin"
        inject = ("routers",)

        async def setup(self, ctx, config):
            router = APIRouter()

            @router.get("/ping")
            def ping():
                return {"pong": True}

            ctx.register_router(router)

    root, registry, _ = _plugin_runtime()
    await registry.register(RouterPlugin)
    routers = root.get("routers")
    assert [route.path for route in routers[0].routes] == ["/ping"]


@pytest.mark.asyncio
async def test_before_agent_run_filter_can_insert_messages():
    events = EventHub()

    def rewrite(ctx):
        ctx.messages.insert(0, {"role": "system", "content": "persona: Alice"})
        ctx.messages.insert(1, {"role": "user", "content": "群规"})
        return ctx

    events.on(BEFORE_AGENT_RUN, rewrite)
    ctx = AgentRunContext(
        session_id="sess_1",
        channel_id="ws",
        messages=[{"role": "system", "content": "base"}],
        config=AgentConfig(),
    )
    ctx = await events.filter(BEFORE_AGENT_RUN, ctx)
    assert [message["content"] for message in ctx.messages] == [
        "persona: Alice",
        "群规",
        "base",
    ]


class _FakeWorkspace(WorkspaceAccessor):
    def __init__(self, cwd: str):
        self.cwd = cwd

    def get(self) -> str:
        return self.cwd


def _plugin_runtime():
    root = FtreContext()
    tools = ToolRegistry()
    root.provide("tool_registry", tools)
    root.provide("routers", [])
    return root, PluginRegistry(root), tools
