"""基于官方 Cordis Context 的类型化 Hook 注册与诊断适配。

本文件解决的是一个很具体的问题：业务 Plugin 需要“在某个稳定时机插入行为”，
但又不应该直接依赖 Cordis EventsService 的内部参数约定。``HookRuntime`` 把两者
隔开：业务侧只需要拿着 Owner、HookSpec 和 listener 注册/dispatch，Kernel 侧负责：

* 校验同名 Hook 是否使用同一份契约；
* 把 ``EMIT``、``PARALLEL``、``SERIAL``、``BAIL``、``WATERFALL`` 映射到 Cordis；
* 给监听器生成可撤销的 ``HookReceipt``；
* 记录失败诊断，并在卸载时等待异步 listener 排空；
* 对 Agent 作用域使用 isolate Context 做身份过滤。

它不保存 Session、Message、Queue、Prompt 或 Tool 等业务状态，也不决定具体 Hook
的业务含义。业务 HookSpec 的定义应该留在对应 Service/Package 中；本 Runtime
只关心“如何可靠地调度”。
"""

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
    """一次监听器注册的可审计回执。

    ``dispose`` 是幂等的取消函数：Plugin 可以主动调用它，Cordis Fiber 在卸载
    时也会调用同一个生命周期 disposer。回执刻意只暴露元数据和 disposer，不暴露
    listener callable，避免外部绕过 HookRuntime 直接调用内部行为。
    """

    hook: str  # HookSpec.name。
    owner: str  # 注册方 Plugin/Service 的稳定 Owner 名。
    mode: HookMode  # 调度模式的快照。
    scope: str  # 诊断标签；真正的 Agent 隔离由 Context identity 完成。
    listener_order: int  # 注册顺序，用于解释 prepend 和调用先后。
    once: bool  # 是否是一次性监听器。
    dispose: Callable[[], Any]  # 幂等取消函数，可能返回 awaitable。


@dataclass(slots=True)
class _Registration:
    """Runtime 自己维护的一条监听器元数据。

    Cordis 内部持有真正的回调和 Fiber Effect；这里不复制回调，只记录审计和
    生命周期所需的少量信息。``active_calls`` 与 ``drained`` 组成卸载屏障：
    删除监听器后，已经进入的异步调用仍然要结束，Plugin 才算完全退出。
    """

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
        # 注册完成时没有 in-flight 调用，初始状态就是“已排空”。只有 listener
        # 真正开始执行时才清空事件，这样 unload 等待的是实际生命周期屏障，
        # 而不是只把回调从 Cordis 列表里删除。
        self.drained.set()


class HookRuntime:
    """把类型化 HookSpec 映射到官方 Cordis Events。

    Runtime 不拥有 Fiber；``ctx.on/once`` 仍由官方 Cordis 绑定当前 Fiber，
    因而 Plugin unload 时监听器会按官方 Effect 语义注销。这里仅负责契约
    校验、once/prepend、scope carrier、失败策略和诊断，不复制事件状态机。

    一次典型调用链是：

    ``Plugin.apply`` → ``register`` → Cordis ``Context.on/once``
    → 业务 Owner 在时机到来时调用 ``dispatch``
    → Cordis 按 HookMode 调 listener → ``dispose``/Fiber unload 清理。

    ``_registrations`` 只是诊断镜像，不能当作第二个事件注册中心；真正的调用
    顺序、事件快照和 Fiber 归属仍以 Cordis 为准。
    """

    def __init__(self, ctx: Context) -> None:
        # 一个 Composition 只有一个根 HookRuntime。业务 Plugin 共享它，但每个
        # listener 仍绑定到注册时的 Cordis Context/Fiber。
        self._ctx = ctx
        # _specs 只保存“某个 Hook 名对应哪份契约”，用来阻止同名不同协议。
        self._specs: dict[str, HookSpec] = {}
        # _registrations 记录顺序、Owner、scope 和 in-flight 数，供审计使用。
        self._registrations: dict[str, list[_Registration]] = {}
        # 失败记录是进程内快照；payload 从不进入这里。
        self._diagnostics: list[HookDiagnostic] = []

    @property
    def diagnostics(self) -> tuple[HookDiagnostic, ...]:
        """返回不可变的失败诊断快照。

        返回 tuple 是为了防止调用方通过 ``append`` 修改 Runtime 的内部诊断
        列表。该列表用于解释 Hook 失败，不是通用日志系统，也不会自动持久化。
        """
        return tuple(self._diagnostics)

    def context_for_scope(self, carrier: HookScopeCarrier) -> Context:
        """为 Agent scope 创建 Cordis Context 视图。

        调用方拿到的是带身份过滤的 isolate Context，不能借此创建新的 Runtime。
        Agent Registry 通常为每个 Agent 生命周期创建一个 carrier，然后把该
        Context 传给 ``register`` 或 ``dispatch``。
        """
        return context_for_scope(self._ctx, carrier)

    def snapshot(self, hook: str | None = None) -> tuple[HookListenerSnapshot, ...]:
        """返回监听器注册顺序快照。

        ``hook`` 为空时返回所有 Hook；传入具体名称时只返回该 Hook。快照不会
        暴露 listener callable、payload 或 Cordis 内部对象，适合用于诊断页面和
        生命周期测试。
        """
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
        all_agent_scopes: bool = False,
    ) -> HookReceipt:
        """注册一个监听器并返回幂等 disposer。

        ``context`` 是 scope carrier：Plugin 必须传入自己的 Cordis Context；全局
        Hook 也不能静默落到根 Context。Agent Hook 传入由 Agent identity 派生的
        isolate Context。诊断 scope 从 Context/策略推导，调用方不能再传入第二份字符串。

        参数语义：

        * ``owner``：声明谁拥有这条行为，卸载诊断和资源清理都依赖它；
        * ``prepend``：只表达“放到现有监听器之前”，不引入任意数字优先级；
        * ``once``：交给 Cordis 的一次性监听器语义处理；
        * ``all_agent_scopes``：Agent-scoped Hook 的全局策略是否也要收到所有
          isolate dispatch；普通 Agent listener 不应随意打开它。

        注册后要么由调用方保存 receipt 并主动 dispose，要么依靠当前 Fiber 的
        Effect 自动清理；两条路径都是幂等的。
        """
        if not owner.strip():
            raise ValueError("Hook owner must be non-empty")
        if not callable(listener):
            raise TypeError("Hook listener must be callable")
        if spec.scope is HookScope.AGENT and context is None and not all_agent_scopes:
            raise ValueError(f"{spec.name} requires an agent scope context")

        # 同一个 Hook 名必须永远对应同一份契约。否则不同 Plugin 可能用不同
        # payload/result 解释同一事件，错误通常只会在运行时深处暴露。
        previous = self._specs.get(spec.name)
        if previous is not None and previous != spec:
            raise ValueError(f"conflicting HookSpec for {spec.name}")
        self._specs[spec.name] = spec

        entries = self._registrations.setdefault(spec.name, [])
        # Fiber restart 会同步删除 Cordis callback；这里同时清掉对应的镜像记录，
        # 再分配顺序，避免 Plugin reload 后诊断里出现已经不存在的“幽灵监听器”。
        entries[:] = [entry for entry in entries if not entry.lifecycle_disposed]
        registration = _Registration(
            spec=spec,
            owner=owner,
            scope=("global" if all_agent_scopes or context is None else "agent"),
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

        # 全局监听器挂根 Context；Agent 监听器挂 isolate Context。两者都由
        # Cordis 自己拥有 Fiber Effect，Runtime 不另建生命周期树。
        registration_context = context or self._ctx
        options: dict[str, Any] = {"prepend": prepend}
        if spec.scope is HookScope.GLOBAL or all_agent_scopes:
            # 全局监听器也必须能收到 scoped dispatch；Cordis 用 global 选项
            # 让它不被 isolate 过滤掉。
            options["global"] = True
        wrapped = self._wrap_listener(spec, listener, registration)
        register = registration_context.once if once else registration_context.on
        disposer = register(spec.name, wrapped, options)
        disposed = False

        def dispose() -> Any:
            # dispose 可能同时被用户代码和 Fiber Effect 调用，所以先用闭包
            # 状态做一次幂等保护；第二次调用不再触碰底层 Cordis disposer。
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            registration.disposed = True
            result = disposer()
            if registration.active_calls:
                # 从 Cordis 列表移除只阻止“新的”调用，不能取消已经开始的
                # async listener。返回一个 awaitable，让上层可以等待它排空。
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
            # 该标记用于下次 register 时清理 Runtime 的诊断镜像；它表示
            # Fiber 生命周期结束，而不只是某个调用方暂时 dispose。
            registration.lifecycle_disposed = True
            return dispose()

        # Context.on/once 已经安装了 Cordis 自己的 Fiber Effect。这个 companion
        # Effect 只负责记录 Runtime 元数据并等待 in-flight async Hook；真正移除
        # callback 的权威仍是 Cordis disposer。两个 disposer 都是幂等的。
        registration_context.effect(
            lambda: lifecycle_dispose,
            label=f"hook:{spec.name}:{owner}",
        )

        return HookReceipt(
            hook=spec.name,
            owner=owner,
            mode=spec.mode,
            scope=registration.scope,
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
        """按 HookSpec 调用官方 Cordis 的对应模式并返回结果。

        ``context`` 为空表示根/全局 dispatch；传入 Agent isolate Context 时，
        Cordis 会根据 Context identity 决定哪些 scoped listener 可以收到事件。
        ``EMIT`` 是观察型广播，不等待 listener 返回值；``PARALLEL`` 并发等待，
        ``SERIAL`` 顺序等待，``BAIL`` 在 Cordis 规则下尽早返回，``WATERFALL``
        通过 ``next_`` 串联前后处理并从 ``spec.default`` 开始。

        dispatch 只做一次 payload/result 校验，不改变 payload，也不对业务结果
        做转换；结果协议由定义 HookSpec 的 Service/Package 负责。
        """
        self._validate_spec(spec, payload, context)
        events = self._ctx.events
        args: tuple[Any, ...]
        if context is None:
            args = (spec.name, payload)
        else:
            # Cordis EventsService 的第一个参数是 scope Context，随后才是
            # 事件名和 payload；不能把 ftre 自己的字符串 scope 传进去。
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
        """在真正触发事件前检查契约和作用域。

        注册过的同名 Spec 不允许被替换；Agent-scoped Hook 没有 isolate Context
        时拒绝 dispatch，避免事件意外落入根 Context 或其他 Agent。
        """
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
        """给 listener 包一层生命周期计数和失败策略。

        Cordis 调用的是这个 wrapper，而不是业务 listener 本身。wrapper 统一处理
        同步/异步回调、EMIT 的 fire-and-forget、once 标记、in-flight 计数和异常
        记录，因此业务 Plugin 不需要各自实现这些容易出错的清理代码。
        """
        def invoke(*args: Any) -> Any:
            # 先计数再调用，确保一个刚进入的 async listener 会被 unload 屏障
            # 看见；active_calls 回到 0 后才重新 set drained。
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
                    # Cordis emit 是同步 API，并且有意忽略回调返回值。若 listener
                    # 返回 coroutine，必须主动 create_task，否则 coroutine 会泄漏；
                    # _finish_async 仍会负责诊断和排空计数。
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
        """等待异步 listener，并把异常/完成统一接回生命周期计数。"""
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
        """减少 in-flight 计数，并在最后一个调用结束时打开排空屏障。"""
        registration.active_calls -= 1
        if registration.active_calls <= 0:
            registration.active_calls = 0
            registration.drained.set()

    def _guard_waterfall_next(
        self, args: tuple[Any, ...], registration: _Registration
    ) -> tuple[Any, ...]:
        """限制 Waterfall listener 的 next_ 只能调用一次。

        Waterfall 的 next_ 是继续调用链的唯一入口。重复调用会让同一个下游链
        被执行两次，结果和副作用都不可预测，因此 Runtime 在边界处显式拒绝。
        """
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
        """记录脱敏诊断，并按 HookSpec 的失败策略决定是否重新抛出。"""
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
            # OBSERVE 适合日志/指标等旁观者：它不能让一个可选 Plugin 的失败
            # 阻断主流程。PROPAGATE 则保留异常，让关键控制 Hook 快速失败。
            return None
        raise exc

    @staticmethod
    async def _await_if_needed(value: Any) -> Any:
        """兼容同步/异步的 Waterfall 最终结果。"""
        if inspect.isawaitable(value):
            return await value
        return value


__all__ = ["HookDiagnostic", "HookListenerSnapshot", "HookReceipt", "HookRuntime"]
