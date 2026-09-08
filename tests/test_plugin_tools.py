import asyncio
from types import SimpleNamespace

import pytest
from cordis import Context, FiberState
from fastapi import APIRouter
from ftre_agent import AgentRegistry, AgentSubject
from ftre_agent.event import HintBlockEvent
from ftre_agent.message import UserMsg
from ftre_agent.tool import ToolDefinition

from ftre.kernel.hooks import HookRuntime
from ftre.plugins.builtin.core_tools import create_read_tool
from ftre.services.attachment import AttachmentService
from ftre.services.http.service import HttpService
from ftre.services.system_prompt.hooks import (
    SYSTEM_PROMPT_ASSEMBLE_SPEC,
    PromptAssemblyPayload,
)
from ftre.services.system_prompt.service import SystemPromptService
from ftre.services.system_prompt.types import PromptSection
from ftre.services.tools import ToolService
from ftre.services.workspace.accessor import WorkspaceAccessor


def _dummy_tool(name: str = "dummy") -> ToolDefinition:
    def dummy() -> str:
        return "ok"

    return ToolDefinition(name=name, description="dummy tool", parameters=[], func=dummy)


def test_tool_service_rejects_duplicate_names_in_one_scope():
    service = ToolService()
    service.register(_dummy_tool("dup"), owner="test")
    with pytest.raises(ValueError, match="already registered"):
        service.register(_dummy_tool("dup"), owner="test")


def test_read_tool_reads_relative_image_path(tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
        b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    result = create_read_tool().execute_callable(
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
    tools = ToolService()
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
    tools = ToolService()
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


@pytest.mark.asyncio
async def test_system_prompt_hook_receives_json_messages_from_typed_runtime_context():
    runtime = HookRuntime(Context())
    service = SystemPromptService()
    registry = AgentRegistry()
    registry.ensure("default")
    seen = []

    async def observe(payload, next_):
        seen.append(payload.messages)
        return await next_()

    runtime.register(
        SYSTEM_PROMPT_ASSEMBLE_SPEC,
        observe,
        owner="prompt-test",
        all_agent_scopes=True,
    )

    await service.assemble_agent_prompt(
        agent_subject=AgentSubject("default", registry.scope_identity("default")),
        session_id="sess_1",
        workspace="E:/repo",
        messages=(UserMsg(content="hello"),),
        base_prompt="",
        inbound_data={},
        config=SimpleNamespace(),
        hook_runtime=runtime,
        scope_context=runtime.context_for_scope(registry.scope_carrier("default")),
        cancellation=asyncio.Event(),
    )

    assert len(seen) == 1
    assert isinstance(seen[0][0], dict)
    assert seen[0][0]["role"] == "user"


class _FakeWorkspace(WorkspaceAccessor):
    def __init__(self, cwd: str):
        self.cwd = cwd

    def get(self) -> str:
        return self.cwd
