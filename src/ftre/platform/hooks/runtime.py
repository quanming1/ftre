"""基于官方 Cordis Context 的类型化 Hook 注册与诊断适配。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cordis import Context

from .diagnostics import HookDiagnostic, HookListenerSnapshot
from .scope import HookScopeCarrier, context_for_scope
from .spec import HookFailurePolicy, HookMode, HookScope, HookSpec


@dataclass(frozen=True, slots=True)
class HookReceipt:
    """一次监听器注册的可审计回执。"""

    hook: str
    owner: str
    mode: HookMode
    scope: str
    listener_order: int
    once: bool
    dispose: Callable[[], Any]


@dataclass(slots=True)
class _Registration:
    spec: HookSpec
    owner: str
    scope: str
    order: int
    once: bool
    active_calls: int = 0
    disposed: bool = False
    lifecycle_disposed: bool = False
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        # A registration starts quiescent.  The event is cleared only while a
        # listener invocation is in flight, allowing Fiber disposal to await a
        # real lifecycle barrier instead of merely removing a callback.
        self.drained.set()


class HookRuntime:
    """把类型化 HookSpec 映射到官方 Cordis Events。

    Runtime 不拥有 Fiber；``ctx.on/once`` 仍由官方 Cordis 绑定当前 Fiber，
    因而 Plugin unload 时监听器会按官方 Effect 语义注销。这里仅负责契约
    校验、once/prepend、scope carrier、失败策略和诊断，不复制事件状态机。
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._specs: dict[str, HookSpec] = {}
        self._registrations: dict[str, list[_Registration]] = {}
        self._diagnostics: list[HookDiagnostic] = []

    @property
    def diagnostics(self) -> tuple[HookDiagnostic, ...]:
        """返回不可变的失败诊断快照。"""
        return tuple(self._diagnostics)

    def context_for_scope(self, carrier: HookScopeCarrier) -> Context:
        """Create a Cordis scope context without exposing the runtime root."""
        return context_for_scope(self._ctx, carrier)

    def snapshot(self, hook: str | None = None) -> tuple[HookListenerSnapshot, ...]:
        """返回注册顺序快照，不暴露 listener callable 或 payload。"""
        registrations = (
            self._registrations.get(hook, [])
            if hook is not None
            else [
                registration
                for entries in self._registrations.values()
                for registration in entries
            ]
        )
        return tuple(
            HookListenerSnapshot(
                hook=registration.spec.name,
                owner=registration.owner,
                mode=registration.spec.mode,
                scope=registration.scope,
                listener_order=registration.order,
                once=registration.once,
                active_calls=registration.active_calls,
                disposed=registration.disposed,
            )
            for registration in registrations
        )

    def register(
        self,
        spec: HookSpec,
        listener: Callable[..., Any],
        *,
        owner: str,
        prepend: bool = False,
        once: bool = False,
        context: Context | None = None,
        scope: str = "global",
        global_listener: bool = False,
    ) -> HookReceipt:
        """注册一个监听器并返回幂等 disposer。

        ``context`` 是 scope carrier：全局 Hook 使用根 Context；Agent Hook
        传入由 Agent identity 派生的 Cordis isolate Context。scope 文本只用于
        诊断，不能替代 Context 对象的身份隔离。
        """
        if not owner.strip():
            raise ValueError("Hook owner must be non-empty")
        if not scope.strip():
            raise ValueError("Hook scope must be non-empty")
        if not callable(listener):
            raise TypeError("Hook listener must be callable")
        if spec.scope is HookScope.AGENT and context is None and not global_listener:
            raise ValueError(f"{spec.name} requires an agent scope context")

        previous = self._specs.get(spec.name)
        if previous is not None and previous != spec:
            raise ValueError(f"conflicting HookSpec for {spec.name}")
        self._specs[spec.name] = spec

        entries = self._registrations.setdefault(spec.name, [])
        # Fiber restart removes Cordis callbacks synchronously.  Prune the
        # corresponding runtime records before assigning the next order so a
        # reload does not accumulate phantom listeners in diagnostics.
        entries[:] = [entry for entry in entries if not entry.lifecycle_disposed]
        registration = _Registration(
            spec=spec,
            owner=owner,
            scope=scope,
            order=(0 if prepend else len(entries)),
            once=once,
        )
        if prepend:
            for entry in entries:
                entry.order += 1
            entries.insert(0, registration)
        else:
            registration.order = len(entries)
            entries.append(registration)

        registration_context = context or self._ctx
        options: dict[str, Any] = {"prepend": prepend}
        if spec.scope is HookScope.GLOBAL or global_listener:
            # Global listeners must also receive scoped dispatches.
            options["global"] = True
        wrapped = self._wrap_listener(spec, listener, registration)
        register = registration_context.once if once else registration_context.on
        disposer = register(spec.name, wrapped, options)
        disposed = False

        def dispose() -> Any:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            registration.disposed = True
            result = disposer()
            if registration.active_calls:
                async def wait_for_quiescence() -> None:
                    await registration.drained.wait()

                if inspect.isawaitable(result):
                    async def wait_then_dispose() -> None:
                        await result
                        await wait_for_quiescence()

                    return wait_then_dispose()
                return wait_for_quiescence()
            return result

        def lifecycle_dispose() -> Any:
            registration.lifecycle_disposed = True
            return dispose()

        # ``Context.on`` installs its own Fiber effect.  This companion effect
        # records the runtime disposition and waits for in-flight async hooks;
        # the underlying Cordis disposer remains the source of truth for event
        # removal.  Both disposers are idempotent.
        registration_context.effect(
            lambda: lifecycle_dispose,
            label=f"hook:{spec.name}:{owner}",
        )

        return HookReceipt(
            hook=spec.name,
            owner=owner,
            mode=spec.mode,
            scope=scope,
            listener_order=registration.order,
            once=once,
            dispose=dispose,
        )

    async def dispatch(
        self,
        spec: HookSpec,
        payload: Any,
        *,
        context: Context | None = None,
    ) -> Any:
        """按 HookSpec 调用官方 Cordis 的对应模式。"""
        self._validate_spec(spec, payload, context)
        events = self._ctx.events
        args: tuple[Any, ...]
        if context is None:
            args = (spec.name, payload)
        else:
            # EventsService 的第一个参数是 scope carrier，随后才是事件名。
            args = (context, spec.name, payload)

        if spec.mode is HookMode.EMIT:
            events.emit(*args)
            return None
        if spec.mode is HookMode.PARALLEL:
            result = await events.parallel(*args)
        elif spec.mode is HookMode.SERIAL:
            result = await events.serial(*args)
        elif spec.mode is HookMode.BAIL:
            result = events.bail(*args)
        else:
            result = events.waterfall(*args, inner=spec.default)
            result = await self._await_if_needed(result)
        spec.validate_result(result)
        return result

    def _validate_spec(
        self, spec: HookSpec, payload: Any, context: Context | None
    ) -> None:
        registered = self._specs.get(spec.name)
        if registered is not None and registered != spec:
            raise ValueError(f"conflicting HookSpec for {spec.name}")
        if spec.scope is HookScope.AGENT and context is None:
            raise ValueError(f"{spec.name} requires an agent scope context")
        spec.validate_payload(payload)

    def _wrap_listener(
        self,
        spec: HookSpec,
        listener: Callable[..., Any],
        registration: _Registration,
    ) -> Callable[..., Any]:
        def invoke(*args: Any) -> Any:
            registration.active_calls += 1
            if registration.active_calls == 1:
                registration.drained.clear()
            call_args = args
            if spec.mode is HookMode.WATERFALL:
                call_args = self._guard_waterfall_next(args, registration)
            try:
                result = listener(*call_args)
            except Exception as exc:  # noqa: BLE001 - policy decides propagation
                self._release_call(registration)
                return self._handle_failure(spec, registration, exc)
            if inspect.isawaitable(result):
                if spec.mode is HookMode.EMIT:
                    # Cordis ``emit`` is intentionally synchronous and ignores
                    # callback return values.  Schedule async observers here so
                    # their coroutine is not leaked while still participating
                    # in active-call diagnostics and Fiber quiescence.
                    asyncio.create_task(
                        self._finish_async(result, spec, registration)
                    )
                    return None
                return self._finish_async(result, spec, registration)
            self._release_call(registration)
            if registration.once:
                registration.disposed = True
            return result

        return invoke

    async def _finish_async(
        self,
        result: Any,
        spec: HookSpec,
        registration: _Registration,
    ) -> Any:
        try:
            return await result
        except Exception as exc:  # noqa: BLE001 - policy decides propagation
            return self._handle_failure(spec, registration, exc)
        finally:
            self._release_call(registration)
            if registration.once:
                registration.disposed = True

    @staticmethod
    def _release_call(registration: _Registration) -> None:
        registration.active_calls -= 1
        if registration.active_calls <= 0:
            registration.active_calls = 0
            registration.drained.set()

    def _guard_waterfall_next(
        self, args: tuple[Any, ...], registration: _Registration
    ) -> tuple[Any, ...]:
        if not args or not callable(args[-1]):
            raise TypeError(f"{registration.spec.name} listener must accept next_()")
        original_next = args[-1]
        called = False

        def next_once() -> Any:
            nonlocal called
            if called:
                raise RuntimeError(f"{registration.spec.name} next_() called twice")
            called = True
            return original_next()

        return (*args[:-1], next_once)

    def _handle_failure(
        self, spec: HookSpec, registration: _Registration, exc: Exception
    ) -> Any:
        self._diagnostics.append(
            HookDiagnostic(
                hook=spec.name,
                owner=registration.owner,
                mode=spec.mode,
                scope=registration.scope,
                listener_order=registration.order,
                active_calls=registration.active_calls,
                exception_type=type(exc).__name__,
                message="listener raised an exception",
            )
        )
        if spec.failure_policy is HookFailurePolicy.OBSERVE:
            return None
        raise exc

    @staticmethod
    async def _await_if_needed(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value


__all__ = ["HookDiagnostic", "HookListenerSnapshot", "HookReceipt", "HookRuntime"]
