from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from cordis import Context
from ftre_agent import (
    AGENT_STOP_DECISION_SPEC,
    AgentRegistry,
    AgentSubject,
    ToolDefinition,
)
from ftre_agent.hooks import (
    ContinueTurn,
    StopDecisionPayload,
    ToolAfterPayload,
    ToolArguments,
    ToolBeforePayload,
    ToolCallIdentity,
    ToolExecutionResult,
)
from ftre_agent.types import ReplyFinishedReason
from ftre_agent_runtime import ReActAgent, ToolCallScheduler
from ftre_agent_runtime.executors.acting import ExitExecutor
from ftre_agent_runtime.run_state import Exit, RunState

from ftre.kernel.hooks import HookRuntime
from ftre.services.llm import LLM_STREAM_SPEC
from ftre.services.session.hooks import SESSION_DISPOSED_SPEC, SessionLifecyclePayload
from ftre.services.system_prompt import SystemPromptService
from ftre.services.system_prompt.hooks import (
    SYSTEM_PROMPT_ASSEMBLE_SPEC,
    PromptAssemblyPayload,
)
from ftre.services.system_prompt.types import PromptSection
from ftre.services.tools import (
    TOOL_AFTER_SPEC,
    TOOL_BEFORE_SPEC,
    ToolService,
)


@pytest.mark.asyncio
async def test_ftre_hook_contracts_have_one_public_owner():
    from ftre_agent import StopDecisionPayload as FtreStopPayload

    from ftre.services.llm.hooks import LlmStreamPayload as FtreLlmPayload
    from ftre.services.tools.hooks import ToolBeforePayload as FtreToolPayload

    assert LLM_STREAM_SPEC.name == "llm/stream"
    assert FtreStopPayload is StopDecisionPayload
    assert FtreLlmPayload.__module__ == "ftre_llm.contracts"
    assert FtreToolPayload.__module__ == "ftre_agent.hooks"


@pytest.mark.asyncio
async def test_core_direct_tool_dispatch_pipeline_uses_cordis_runtime():
    runtime = HookRuntime(Context())
    agent_registry = AgentRegistry()
    agent_registry.ensure("default")
    calls: list[str] = []

    def echo(value: str) -> str:
        calls.append(value)
        return value

    tools = ToolService(hook_runtime=runtime)
    tools.register(ToolDefinition(name="echo", execute=echo), owner="test")
    view = await tools.prepare_view("default", "session-1")
    seen: list[str] = []

    async def before(payload: ToolBeforePayload, next_):
        seen.append(f"pre:{payload.call.call_id}")
        await next_()
        return ToolArguments({"value": "changed"})

    async def after(payload: ToolAfterPayload, next_):
        result = await next_()
        seen.append(f"post:{payload.result.output}")
        return ToolExecutionResult(output=result.output.upper(), value=result.value)

    runtime.register(TOOL_BEFORE_SPEC, before, owner="policy", all_agent_scopes=True)
    runtime.register(TOOL_AFTER_SPEC, after, owner="audit", all_agent_scopes=True)
    call = ToolCallIdentity("call-1", "echo", "session-1", "turn-1", "default")
    context = runtime.context_for_scope(agent_registry.scope_carrier("default"))
    state = RunState()
    state.runtime_context = {
        "session_id": "session-1",
        "cancellation": asyncio.Event(),
    }
    state.start()
    scheduler = ToolCallScheduler(view, hooks=runtime, hook_context=context)
    result = await scheduler.run_one(
        call.call_id,
        call.name,
        {"value": "original"},
        state,
    )
    assert result.result == "CHANGED"
    assert calls == ["changed"]
    assert seen == ["pre:call-1", "post:changed"]


@pytest.mark.asyncio
async def test_stop_decision_core_directly_uses_ftre_runtime():
    runtime = HookRuntime(Context())
    registry = AgentRegistry()
    registry.ensure("default")

    async def continue_work(payload, next_):
        await next_()
        return ContinueTurn("继续完成剩余工作", source="policy")

    runtime.register(AGENT_STOP_DECISION_SPEC, continue_work, owner="policy", all_agent_scopes=True)
    class EmptyView:
        names = ()

        def to_openai_tools(self):
            return []

    class FakeLlm:
        async def stream(self, _messages, _tools=None):
            return
            yield

        def cancel(self):
            return None

    agent = ReActAgent(
        model="fake", api_key="fake", hooks=runtime, tool_view=EmptyView(), llm=FakeLlm()
    )
    state = RunState()
    state.runtime_context = {
        "session_id": "session-1",
        "request_id": "request-1",
        "agent_subject": AgentSubject("default", registry.scope_identity("default")),
        "cancellation": asyncio.Event(),
        "max_continuations": 2,
    }
    state.start()
    state.reply_id = "reply-1"
    context = runtime.context_for_scope(registry.scope_carrier("default"))
    executor = ExitExecutor(agent, state, runtime, context)
    events = [
        event async for event in executor.stream(
            Exit(finished_reason=ReplyFinishedReason.COMPLETED)
        )
    ]
    assert executor.outcome.should_continue is True
    assert events
    assert isinstance(state.runtime_context.get("agent_subject"), AgentSubject)


@pytest.mark.asyncio
async def test_structured_prompt_assembly_is_waterfall_replaceable():
    runtime = HookRuntime(Context())
    service = SystemPromptService()
    service.register_section(PromptSection(name="feature", content="feature"))
    assembly = service.assemble_result("default", "session-1", base_prompt="base")

    async def add_section(payload, next_):
        result = await next_()
        return type(result)(result.agent_id, result.session_id, result.workspace, result.contributions, result.text + "\n\npolicy")

    runtime.register(SYSTEM_PROMPT_ASSEMBLE_SPEC, add_section, owner="policy", all_agent_scopes=True)
    registry = AgentRegistry()
    registry.ensure("default")
    result = await runtime.dispatch(
        SYSTEM_PROMPT_ASSEMBLE_SPEC,
        PromptAssemblyPayload(
            agent=AgentSubject("default", registry.scope_identity("default")),
            session_id="session-1",
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
    assert result.text == "base\n\nfeature\n\npolicy"


@pytest.mark.asyncio
async def test_session_disposed_is_an_awaited_public_lifecycle_fact():
    context = Context()
    runtime = HookRuntime(context)
    seen: list[str] = []

    async def dispatcher(payload):
        await asyncio.sleep(0)
        seen.append(payload.session_id)

    runtime.register(SESSION_DISPOSED_SPEC, dispatcher, owner="test", context=context)
    await runtime.dispatch(
        SESSION_DISPOSED_SPEC,
        SessionLifecyclePayload("session-1", "ws", "test"),
    )
    assert seen == ["session-1"]
    context.dispose()
