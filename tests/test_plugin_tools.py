import asyncio
from types import SimpleNamespace

import pytest
from cordis import Context, FiberState
from fastapi import APIRouter
from ftre_agent_core.event import HintBlockEvent
from ftre_agent_core.tool import Tool, ToolRegistry

from ftre.kernel.hooks import HookRuntime
from ftre.services.agent.hooks import AgentSubject
from ftre.services.agent.registry import AgentRegistry
from ftre.services.attachment import AttachmentService
from ftre.services.http.service import HttpService
from ftre.services.system_prompt.hooks import (
    SYSTEM_PROMPT_ASSEMBLE_SPEC,
    PromptAssemblyPayload,
)
from ftre.services.system_prompt.service import SystemPromptService
from ftre.services.system_prompt.types import PromptSection
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
        attachments=AttachmentService(tmp_path / "assets"),
    )
    assert isinstance(result, HintBlockEvent)
    assert result.metadata["path"] == str(image.resolve())


@pytest.mark.asyncio
async def test_cordis_plugin_failure_rolls_back_registered_tools():
    root = Context()
    tools = ToolService(ToolRegistry())
    root.provide("tools", tools)

    def failing_plugin(ctx, _config=None):
        ctx.effect(lambda: ctx.tools.register(_dummy_tool("leaked"), owner="failing"))
        raise RuntimeError("boom")

    failing_plugin.inject = ("tools",)
    fiber = root.plugin(failing_plugin)
    with pytest.raises(RuntimeError, match="boom"):
        await fiber.await_()
    assert fiber.state is FiberState.FAILED
    assert tools.snapshot() == ()
    cleanup = root.dispose()
    if cleanup is not None:
        await cleanup


@pytest.mark.asyncio
async def test_cordis_plugin_contributions_and_router_are_reversible():
    root = Context()
    tools = ToolService(ToolRegistry())
    http = HttpService()
    root.provide("tools", tools)
    root.provide("http", http)

    def plugin(ctx, _config=None):
        ctx.effect(lambda: ctx.tools.register(_dummy_tool("from_plugin"), owner="plugin"))
        router = APIRouter()

        @router.get("/ping")
        def ping():
            return {"pong": True}

        ctx.effect(lambda: ctx.http.register_router(router, owner="plugin"))

    plugin.inject = ("tools", "http")
    fiber = root.plugin(plugin)
    await fiber
    assert fiber.state is FiberState.ACTIVE
    assert tools.snapshot()[0].name == "from_plugin"
    assert any(item["path"] == "/api/ping" for item in http.snapshot())
    cleanup = root.dispose()
    if cleanup is not None:
        await cleanup
    assert tools.snapshot() == ()
    assert http.snapshot() == ()


@pytest.mark.asyncio
async def test_structured_prompt_hook_replaces_assembly_without_mutable_filter():
    runtime = HookRuntime(Context())
    service = SystemPromptService()
    service.register_section(PromptSection(name="feature", content="feature"))
    assembly = service.assemble_result("default", "sess_1", base_prompt="base")
    registry = AgentRegistry()
    registry.ensure("default")

    async def rewrite(payload, next_):
        result = await next_()
        return type(result)(
            result.agent_id,
            result.session_id,
            result.workspace,
            result.contributions,
            result.text + "\n\npersona: Alice",
        )

    runtime.register(
        SYSTEM_PROMPT_ASSEMBLE_SPEC,
        rewrite,
        owner="prompt-test",
        all_agent_scopes=True,
    )
    result = await runtime.dispatch(
        SYSTEM_PROMPT_ASSEMBLE_SPEC,
        PromptAssemblyPayload(
            agent=AgentSubject("default", registry.scope_identity("default")),
            session_id="sess_1",
            workspace="/tmp",
            assembly=assembly,
            messages=(),
            inbound_data={},
            config=SimpleNamespace(),
            event_loop=None,
            cancellation=asyncio.Event(),
        ),
        context=runtime.context_for_scope(registry.scope_carrier("default")),
    )
    assert result.text.endswith("persona: Alice")


class _FakeWorkspace(WorkspaceAccessor):
    def __init__(self, cwd: str):
        self.cwd = cwd

    def get(self) -> str:
        return self.cwd
