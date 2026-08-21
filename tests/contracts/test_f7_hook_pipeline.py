from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from cordis import Context
from ftre_agent_core.agent import ReActAgent
from ftre_agent_core.agent.runner._execute_acting import ExitExecutor
from ftre_agent_core.agent.runner._state import Exit, RunState
from ftre_agent_core.hooks import (
    AGENT_TURN_STOPPING_SPEC as CORE_STOP_SPEC,
)
from ftre_agent_core.hooks import (
    LLM_STREAM_SPEC as CORE_LLM_SPEC,
)
from ftre_agent_core.hooks import (
    ContinueTurn,
    ToolArguments,
    ToolCallIdentity,
    ToolExecutePayload,
    ToolExecutionResult,
    ToolPreExecutePayload,
    TurnStoppingPayload,
)
from ftre_agent_core.types import ReplyFinishedReason

from ftre.platform.hooks import HookRuntime
from ftre.services.agent.hooks import AGENT_TURN_STOPPING_SPEC, AgentSubject
from ftre.services.agent.registry import AgentRegistry
from ftre.services.llm import LLM_STREAM_SPEC
from ftre.services.session import SessionService
from ftre.services.system_prompt import SystemPromptService
from ftre.services.system_prompt.hooks import (
    SYSTEM_PROMPT_ASSEMBLE_SPEC,
    PromptAssemblyPayload,
)
from ftre.services.system_prompt.types import PromptSection
from ftre.services.tools import (
    TOOLS_EXECUTE_SPEC,
    TOOLS_POST_EXECUTE_SPEC,
    TOOLS_PRE_EXECUTE_SPEC,
    TOOLS_RESULT_SPEC,
)
from ftre.services.tools.hooks import ToolPostExecutePayload


@pytest.mark.asyncio
async def test_ftre_reexports_core_hook_contracts_without_duplicate_owner():
    from ftre.services.agent.hooks import TurnStoppingPayload as FtreStopPayload
    from ftre.services.llm.hooks import LLMStreamPayload as FtreLlmPayload
    from ftre.services.tools.hooks import ToolPreExecutePayload as FtreToolPayload

    assert AGENT_TURN_STOPPING_SPEC is CORE_STOP_SPEC
    assert LLM_STREAM_SPEC is CORE_LLM_SPEC
    assert FtreStopPayload is TurnStoppingPayload
    assert FtreLlmPayload.__module__ == "ftre_agent_core.hooks"
    assert FtreToolPayload.__module__ == "ftre_agent_core.hooks"


@pytest.mark.asyncio
async def test_core_direct_tool_dispatch_pipeline_uses_cordis_runtime():
    runtime = HookRuntime(Context())
    registry = AgentRegistry()
    registry.ensure("default")
    seen: list[str] = []

    async def pre(payload, next_):
        seen.append(f"pre:{payload.call.call_id}")
        await next_()
        return ToolArguments({"value": "changed"})

    async def execute(payload, next_):
        seen.append("execute:before")
        result = await next_()
        seen.append("execute:after")
        return ToolExecutionResult(output=result.output.upper(), value=result.value)

    async def post(payload: ToolPostExecutePayload, next_):
        seen.append(f"post:{payload.result.output}")
        return await next_()

    runtime.register(TOOLS_PRE_EXECUTE_SPEC, pre, owner="policy", global_listener=True)
    runtime.register(TOOLS_EXECUTE_SPEC, execute, owner="wrapper", global_listener=True)
    runtime.register(TOOLS_POST_EXECUTE_SPEC, post, owner="audit", global_listener=True)
    runtime.register(
        TOOLS_RESULT_SPEC,
        lambda payload: seen.append(f"result:{payload.result.output}"),
        owner="result",
        global_listener=True,
    )
    call = ToolCallIdentity("call-1", "echo", "session-1", "turn-1", "default")
    payload = ToolPreExecutePayload(call, {"value": "original"}, asyncio.Event())
    context = runtime.context_for_scope(registry.scope_carrier("default"))
    assert isinstance(
        await runtime.dispatch(TOOLS_PRE_EXECUTE_SPEC, payload, context=context),
        ToolArguments,
    )
    result = await runtime.dispatch(
        TOOLS_EXECUTE_SPEC,
        ToolExecutePayload(
            call,
            {"value": "changed"},
            asyncio.Event(),
            _fake_result,
        ),
        context=context,
    )
    assert result.output == "ORIGINAL"
    assert seen[:3] == ["pre:call-1", "execute:before", "execute:after"]


async def _fake_result():
    return ToolExecutionResult(output="original")


@pytest.mark.asyncio
async def test_turn_stopping_core_directly_uses_ftre_runtime():
    runtime = HookRuntime(Context())
    registry = AgentRegistry()
    registry.ensure("default")

    async def continue_work(payload, next_):
        await next_()
        return ContinueTurn("继续完成剩余工作", source="policy")

    runtime.register(AGENT_TURN_STOPPING_SPEC, continue_work, owner="policy", global_listener=True)
    agent = ReActAgent(model="fake", api_key="fake", hooks=runtime)
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

    runtime.register(SYSTEM_PROMPT_ASSEMBLE_SPEC, add_section, owner="policy", global_listener=True)
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
async def test_session_flush_is_unique_public_barrier():
    session = SessionService()
    seen: list[tuple[str, str]] = []

    async def dispatcher(session_id: str, reason: str):
        seen.append((session_id, reason))

    dispose = session.bind_flush_dispatcher(dispatcher)
    await session.flush("session-1", reason="checkpoint")
    assert seen == [("session-1", "checkpoint")]
    assert dispose() is True
    await session.flush("session-1", reason="after-dispose")
    assert seen == [("session-1", "checkpoint")]
