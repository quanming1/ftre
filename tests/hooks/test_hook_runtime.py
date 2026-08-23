from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest
from cordis import Context

from ftre.platform.hooks import (
    AGENT_BEFORE_TURN,
    HookFailurePolicy,
    HookMode,
    HookRuntime,
    HookScope,
    HookScopeCarrier,
    HookSpec,
    context_for_scope,
)


def _spec(mode: HookMode, *, failure=HookFailurePolicy.PROPAGATE) -> HookSpec:
    async def default(payload):
        return payload

    return HookSpec(
        AGENT_BEFORE_TURN,
        "agent",
        mode,
        failure_policy=failure,
        payload_type=dict,
        default=default,
    )


def test_hook_spec_rejects_unstable_names_and_requires_waterfall_default():
    with pytest.raises(ValueError, match="domain/name"):
        HookSpec("invalid", "agent", HookMode.EMIT, payload_type=dict)
    with pytest.raises(ValueError, match="requires an explicit default"):
        HookSpec("agent/request", "agent", HookMode.WATERFALL, payload_type=dict)
    with pytest.raises(TypeError, match="HookSpec.mode"):
        HookSpec("agent/request", "agent", "serial", payload_type=dict)  # type: ignore[arg-type]


def test_hook_spec_is_immutable():
    spec = _spec(HookMode.EMIT)
    with pytest.raises(FrozenInstanceError):
        spec.name = "agent/changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_waterfall_uses_official_continuation_and_receipt_is_reversible():
    context = Context()
    runtime = HookRuntime(context)
    calls: list[str] = []
    spec = _spec(HookMode.WATERFALL)

    async def listener(payload, next_):
        calls.append("before")
        result = await next_()
        calls.append("after")
        return {**result, "hooked": True}

    receipt = runtime.register(spec, listener, owner="test-plugin")
    assert await runtime.dispatch(spec, {"value": 1}) == {
        "value": 1,
        "hooked": True,
    }
    assert calls == ["before", "after"]
    assert receipt.dispose() is not False
    assert receipt.dispose() is False
    assert await runtime.dispatch(spec, {"value": 2}) == {"value": 2}


@pytest.mark.asyncio
async def test_all_cordis_dispatch_modes_have_explicit_runtime_semantics():
    context = Context()
    runtime = HookRuntime(context)

    emit_seen: list[str] = []
    emit_spec = _spec(HookMode.EMIT)
    runtime.register(emit_spec, lambda _payload: emit_seen.append("emit"), owner="emit")
    assert await runtime.dispatch(emit_spec, {}) is None
    assert emit_seen == ["emit"]

    runtime = HookRuntime(Context())
    parallel_seen: list[str] = []
    parallel_spec = _spec(HookMode.PARALLEL)

    async def parallel_one(_payload):
        parallel_seen.append("one")

    async def parallel_two(_payload):
        parallel_seen.append("two")

    runtime.register(parallel_spec, parallel_one, owner="one")
    runtime.register(parallel_spec, parallel_two, owner="two")
    assert await runtime.dispatch(parallel_spec, {}) is None
    assert sorted(parallel_seen) == ["one", "two"]

    runtime = HookRuntime(Context())
    serial_seen: list[str] = []
    serial_spec = _spec(HookMode.SERIAL)
    runtime.register(serial_spec, lambda _payload: serial_seen.append("one"), owner="one")
    runtime.register(serial_spec, lambda _payload: serial_seen.append("two"), owner="two")
    assert await runtime.dispatch(serial_spec, {}) is None
    assert serial_seen == ["one", "two"]

    runtime = HookRuntime(Context())
    bail_spec = _spec(HookMode.BAIL)
    bail_seen: list[str] = []
    runtime.register(bail_spec, lambda _payload: bail_seen.append("none"), owner="none")
    runtime.register(bail_spec, lambda _payload: "stop", owner="stop")
    runtime.register(bail_spec, lambda _payload: bail_seen.append("late"), owner="late")
    assert await runtime.dispatch(bail_spec, {}) == "stop"
    assert bail_seen == ["none"]


@pytest.mark.asyncio
async def test_waterfall_can_short_circuit_without_running_default():
    context = Context()
    runtime = HookRuntime(context)
    spec = _spec(HookMode.WATERFALL)
    seen: list[str] = []

    async def short_circuit(payload, _next_):
        seen.append("short")
        return {**payload, "stopped": True}

    runtime.register(spec, short_circuit, owner="short")
    result = await runtime.dispatch(spec, {})
    assert result == {"stopped": True}
    assert seen == ["short"]


@pytest.mark.asyncio
async def test_parallel_propagate_preserves_all_failures():
    context = Context()
    runtime = HookRuntime(context)
    spec = _spec(HookMode.PARALLEL)

    async def fail_one(_payload):
        raise ValueError("one")

    async def fail_two(_payload):
        raise RuntimeError("two")

    runtime.register(spec, fail_one, owner="one")
    runtime.register(spec, fail_two, owner="two")
    with pytest.raises(ExceptionGroup) as error:
        await runtime.dispatch(spec, {})
    assert {type(item) for item in error.value.exceptions} == {ValueError, RuntimeError}


@pytest.mark.asyncio
async def test_emit_observer_failure_isolated_and_diagnosed():
    context = Context()
    runtime = HookRuntime(context)
    spec = _spec(HookMode.EMIT, failure=HookFailurePolicy.OBSERVE)
    seen: list[str] = []

    def broken(_payload):
        raise ValueError("private payload must not be logged")

    runtime.register(spec, broken, owner="broken")
    runtime.register(spec, lambda _payload: seen.append("healthy"), owner="healthy")
    await runtime.dispatch(spec, {})
    assert seen == ["healthy"]
    assert runtime.diagnostics[0].message == "listener raised an exception"


@pytest.mark.asyncio
async def test_observer_failure_is_diagnosed_without_blocking_dispatch():
    context = Context()
    runtime = HookRuntime(context)
    spec = _spec(HookMode.PARALLEL, failure=HookFailurePolicy.OBSERVE)
    seen: list[str] = []

    async def broken(_payload):
        raise RuntimeError("redacted-safe")

    async def healthy(_payload):
        seen.append("healthy")

    runtime.register(spec, broken, owner="broken-plugin")
    runtime.register(spec, healthy, owner="healthy-plugin")
    await runtime.dispatch(spec, {})

    assert seen == ["healthy"]
    assert runtime.diagnostics[0].hook == AGENT_BEFORE_TURN
    assert runtime.diagnostics[0].owner == "broken-plugin"
    assert runtime.diagnostics[0].exception_type == "RuntimeError"


@pytest.mark.asyncio
async def test_conflicting_specs_and_payload_types_are_rejected():
    context = Context()
    runtime = HookRuntime(context)
    spec = _spec(HookMode.EMIT)
    runtime.register(spec, lambda _payload: None, owner="plugin")
    with pytest.raises(ValueError, match="conflicting"):
        runtime.register(_spec(HookMode.SERIAL), lambda _payload: None, owner="other")
    with pytest.raises(TypeError, match="payload"):
        # The registered spec requires a dict payload.
        await runtime.dispatch(spec, "wrong")


@pytest.mark.asyncio
async def test_result_type_is_checked_at_the_dispatch_boundary():
    context = Context()
    runtime = HookRuntime(context)
    spec = HookSpec(
        "agent/request",
        "agent",
        HookMode.BAIL,
        payload_type=dict,
        result_type=str,
    )
    runtime.register(spec, lambda _payload: 1, owner="bad-result")
    with pytest.raises(TypeError, match="result"):
        await runtime.dispatch(spec, {})


@pytest.mark.asyncio
async def test_once_prepend_and_listener_snapshot_are_deterministic():
    context = Context()
    runtime = HookRuntime(context)
    spec = _spec(HookMode.PARALLEL)
    seen: list[str] = []

    async def first(_payload):
        seen.append("first")
        runtime.register(spec, lambda _value: seen.append("late"), owner="late")

    async def normal(_payload):
        seen.append("normal")

    runtime.register(spec, normal, owner="normal")
    runtime.register(spec, first, owner="first", prepend=True)
    runtime.register(spec, lambda _value: seen.append("once"), owner="once", once=True)

    assert [entry.listener_order for entry in runtime.snapshot(spec.name)] == [0, 1, 2]
    await runtime.dispatch(spec, {})
    assert seen == ["first", "normal", "once"]
    await runtime.dispatch(spec, {})
    assert seen == ["first", "normal", "once", "first", "normal", "late"]
    assert runtime.snapshot(spec.name)[2].disposed is True


@pytest.mark.asyncio
async def test_waterfall_rejects_duplicate_next_and_records_order():
    context = Context()
    runtime = HookRuntime(context)
    spec = _spec(HookMode.WATERFALL)

    async def duplicate(payload, next_):
        await next_()
        await next_()
        return payload

    runtime.register(spec, duplicate, owner="duplicate")
    with pytest.raises(RuntimeError, match="called twice"):
        await runtime.dispatch(spec, {})
    diagnostic = runtime.diagnostics[0]
    assert diagnostic.listener_order == 0
    assert diagnostic.active_calls == 1
    assert diagnostic.message == "listener raised an exception"


@pytest.mark.asyncio
async def test_agent_scope_uses_context_identity_and_global_observer_sees_both():
    context = Context()
    runtime = HookRuntime(context)
    scoped = _spec(HookMode.PARALLEL)
    scoped = HookSpec(
        scoped.name,
        scoped.domain,
        scoped.mode,
        failure_policy=scoped.failure_policy,
        payload_type=scoped.payload_type,
        default=scoped.default,
        scope=HookScope.AGENT,
    )
    agent_a = context.isolate("agent", object())
    agent_b = context.isolate("agent", object())
    seen: list[str] = []

    runtime.register(
        scoped,
        lambda _payload: seen.append("a"),
        owner="agent-a",
        context=agent_a,
        scope="agent-a",
    )
    runtime.register(
        scoped,
        lambda _payload: seen.append("global"),
        owner="global-observer",
        global_listener=True,
    )
    await runtime.dispatch(scoped, {}, context=agent_a)
    await runtime.dispatch(scoped, {}, context=agent_b)
    assert seen == ["a", "global", "global"]


@pytest.mark.asyncio
async def test_scope_carrier_inherits_parent_and_rejects_rebuilt_same_id():
    context = Context()
    runtime = HookRuntime(context)
    spec = HookSpec(
        AGENT_BEFORE_TURN,
        "agent",
        HookMode.PARALLEL,
        payload_type=dict,
        scope=HookScope.AGENT,
    )
    parent_identity = object()
    child_identity = object()
    rebuilt_identity = object()
    parent = HookScopeCarrier("agent", parent_identity)
    child = HookScopeCarrier("agent", child_identity, parent=parent)
    rebuilt = HookScopeCarrier("agent", rebuilt_identity)
    parent_context = context_for_scope(context, parent)
    child_context = context_for_scope(context, child)
    rebuilt_context = context_for_scope(context, rebuilt)
    seen: list[str] = []

    runtime.register(
        spec,
        lambda _payload: seen.append("parent"),
        owner="parent",
        context=parent_context,
        scope="parent",
    )
    runtime.register(
        spec,
        lambda _payload: seen.append("child"),
        owner="child",
        context=child_context,
        scope="child",
    )
    await runtime.dispatch(spec, {}, context=child_context)
    await runtime.dispatch(spec, {}, context=rebuilt_context)
    assert seen == ["parent", "child"]


@pytest.mark.asyncio
async def test_snapshot_reports_in_flight_listener_and_quiescence():
    context = Context()
    runtime = HookRuntime(context)
    spec = _spec(HookMode.PARALLEL)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(_payload):
        started.set()
        await release.wait()

    runtime.register(spec, blocking, owner="blocking")
    task = asyncio.create_task(runtime.dispatch(spec, {}))
    await started.wait()
    assert runtime.snapshot(spec.name)[0].active_calls == 1
    release.set()
    await task
    assert runtime.snapshot(spec.name)[0].active_calls == 0
