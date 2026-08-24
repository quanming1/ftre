"""把选定的 Plugin 交给官方 cordis-py Fiber 执行。

ftre 负责 Manifest 选择和状态诊断，但 Plugin 的激活、依赖等待、Effect 注册、
卸载和重启都交给官方 Cordis ``Fiber``/``Registry``。本文件不能再实现一套自研
Plugin 状态机，否则同一个 Plugin 会出现两套 ACTIVE、PENDING、dispose 语义，
最终造成资源泄漏或状态不一致。

这里保留的代码主要是三类适配：把 ftre 支持的入口形状包成 Cordis 可调用对象，
把异步 cleanup 统一 await，以及把 Fiber 状态转换成 ftre 的稳定诊断对象。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from cordis import Context, Fiber, FiberState

from .diagnostics import PluginStartupError, PluginStatus
from .discovery import PluginDiscovery
from .manifest import PluginManifest


class _ManifestEntry:
    """给一次 Manifest 实例命名的 Cordis 可调用适配器。

    Cordis 需要一个有稳定 ``name`` 的 plugin entry；而 ftre 的 Manifest 还允许
    函数、类、带 ``apply`` 的对象和 ``module:attribute`` 解析结果。这个私有适配器
    只负责统一入口调用，不拥有 Service、Fiber 或业务配置。
    """

    def __init__(self, plugin: Any, plugin_id: str) -> None:
        # 保存原始入口以便调用；name/inject/Config 是 Cordis 读取的声明元数据。
        self._plugin = plugin
        self.name = plugin_id
        self.inject = getattr(plugin, "inject", ()) or ()
        config = getattr(plugin, "Config", None)
        if config is not None:
            self.Config = config

    def __call__(self, ctx: Context, config: Any = None) -> Any:
        """在官方 Fiber 加载流程中调用 ftre 支持的入口形状。

        优先使用对象的 ``apply`` 方法；否则把对象自身当作 callable。类入口会先
        尝试无参构造，再兼容 ``(ctx, config)`` 构造形式。真正的生命周期仍由
        Cordis Fiber 管理，这个方法不负责保存实例或捕获异常。
        """
        target = self._plugin
        if isinstance(target, type):
            # 大多数 Plugin 是无参类，依赖通过 ctx 注入；旧的可调用类仍允许
            # 接收 ctx/config，但最终错误要交给 Fiber 诊断，不在这里吞掉。
            try:
                target = target()
            except TypeError:
                try:
                    target = target(ctx, config)
                except TypeError:
                    # Let the final callable/apply validation report the exact
                    # unsupported class shape through the Fiber diagnostics.
                    pass
        apply = getattr(target, "apply", None)
        if callable(apply):
            return _call_entry(apply, ctx, config)
        if callable(target):
            return _call_entry(target, ctx, config)
        raise TypeError(f"plugin {self.name!r} has no callable apply entry")


def _call_entry(entry: Callable[..., Any], ctx: Context, config: Any) -> Any:
    """兼容文档约定的两参数入口和简洁的一参数入口。

    入口通常是 ``apply(ctx, config)``，也允许只写 ``apply(ctx)``。这里通过签名
    检查选择调用方式，不使用“先调用、捕获 TypeError、再重试”的方式，因为那
    会把入口函数内部真正抛出的 TypeError 误判成参数数量问题。
    """
    try:
        signature = inspect.signature(entry)
    except (TypeError, ValueError):
        return entry(ctx, config)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    accepts_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if accepts_varargs or len(positional) >= 2:
        return entry(ctx, config)
    return entry(ctx)


async def _await_maybe(awaitable: Any) -> None:
    """等待可能为异步的 Cordis 清理句柄。"""
    if inspect.isawaitable(awaitable):
        await awaitable


class PluginLoader:
    """把已选 Manifest 转换成官方 cordis-py Fiber。

    Loader 只追踪自己创建的 Fiber、Manifest 和错误，以便生成状态；它不复制
    Cordis 的依赖图。依赖缺失时 Fiber 可以处于 PENDING，等待其他 Provider 出现，
    而不是由 Loader 手工排序或主动 ``ctx.get`` 解决。
    """

    def __init__(self, context: Context, *, discovery: PluginDiscovery | None = None) -> None:
        # Context 是 Composition 的根；Loader 不创建第二个 Context/Registry。
        self.context = context
        self.discovery = discovery or PluginDiscovery()
        self._fibers: dict[str, Fiber] = {}
        self._entries: dict[str, _ManifestEntry] = {}
        self._manifests: dict[str, PluginManifest] = {}
        self._errors: dict[str, BaseException] = {}
        self._started_at: dict[str, float] = {}
        self.restart_required = False

    async def load(self, manifests: list[PluginManifest]) -> tuple[PluginStatus, ...]:
        """解析、注册、等待 Fiber，并执行 required Plugin 启动门禁。

        流程是：先解析每个已选 Manifest，再交给 ``context.plugin``；由 Cordis
        根据 inject/provide 和 Fiber Effect 管理激活；等待当前 Fiber 加载完成；
        最后生成状态并检查 required 是否全部 ACTIVE。

        可选 Plugin 失败会留在状态中但不阻止启动；required Plugin 失败会先清理
        根 Context，再抛出带完整 statuses 的 ``PluginStartupError``。
        """
        for manifest in manifests:
            if manifest.id in self._manifests:
                raise ValueError(f"plugin {manifest.id!r} is already loaded")
            self._manifests[manifest.id] = manifest
            self._started_at[manifest.id] = time.perf_counter()
            try:
                # resolve 可能 import 外部模块，只对被 Manager 选中的 Manifest
                # 执行；配置验证是 Plugin 自己声明的可选边界。
                entry = self.discovery.resolve(manifest)
                plugin_config = manifest.config
                validate = getattr(entry, "validate_config", None)
                if callable(validate):
                    plugin_config = validate(plugin_config)
                named_entry = _ManifestEntry(entry, manifest.id)
                # context.plugin 是唯一的激活入口：依赖等待、Fiber Effect 和
                # apply 的异常都归官方 Cordis 管理。
                fiber = self.context.plugin(named_entry, plugin_config)
                self._entries[manifest.id] = named_entry
                self._fibers[manifest.id] = fiber
            except Exception as exc:  # noqa: BLE001 - retain import/setup diagnostics
                # 入口导入/配置校验失败时没有 Fiber 可等待，但必须保留错误，
                # 让 statuses 区分 entry_import_failed 与 apply_failed。
                self._errors[manifest.id] = exc

        # 官方 cordis 没有 ftre 自定义的 Context.settle。逐个等待 Fiber 只排空
        # 它自身的 loading inertia；依赖 epoch 仍由 ReflectService/Fiber 持有。
        await self._await_fibers()
        statuses = self.statuses()
        required_failures = [
            status
            for status in statuses
            if status.required and status.state not in {FiberState.ACTIVE, "ACTIVE"}
        ]
        if required_failures:
            await _await_maybe(self.context.dispose())
            raise PluginStartupError("required plugin startup failed", statuses)
        return statuses

    async def _await_fibers(self) -> None:
        """等待当前已注册的全部 Fiber，并保留各自失败原因。

        一个 Fiber 失败不能让其他 Fiber 的状态完全丢失；这里逐个等待并把异常
        写入对应 Plugin 的错误表，最后由 required 门禁统一裁决。
        """
        for plugin_id, fiber in self._fibers.items():
            try:
                await fiber.await_()
            except BaseException as exc:  # noqa: BLE001 - status owns failure reporting
                self._errors.setdefault(plugin_id, exc)

    async def unload(self, plugin_id: str) -> bool:
        """卸载一个官方 Fiber，并标记需要重启才能收回的 Host 表面。

        先从 Loader 的追踪表移除句柄，再等待 Fiber dispose；Fiber 自己负责撤销
        该 Plugin 注册的 Effect、Service 和监听器。HTTP/WebSocket 这类不可热替换
        的 Host 表面会设置 ``restart_required``，提醒调用方不要把“卸载成功”误解
        成“进程路由已经完全重建”。
        """
        fiber = self._fibers.pop(plugin_id, None)
        self._entries.pop(plugin_id, None)
        self._manifests.pop(plugin_id, None)
        self._errors.pop(plugin_id, None)
        self._started_at.pop(plugin_id, None)
        if fiber is None:
            return False
        await _await_maybe(fiber.dispose())
        self.restart_required = self.restart_required or plugin_id.startswith(("http", "websocket"))
        return True

    async def restart(self, plugin_id: str) -> bool:
        """使用官方 cordis-py 生命周期重启一个已加载的 Fiber。"""
        fiber = self._fibers.get(plugin_id)
        if fiber is None:
            return False
        self._errors.pop(plugin_id, None)
        try:
            await fiber.restart()
        except BaseException as exc:  # noqa: BLE001 - expose through diagnostics
            self._errors[plugin_id] = exc
        return fiber.state is FiberState.ACTIVE

    async def dispose(self) -> None:
        """关闭根 Context，并清空 Loader 自己持有的句柄。

        根 Context 的 dispose 会级联清理所有 Plugin Fiber；清空这些字典只是避免
        Loader 在关闭后继续暴露旧状态，不是另一套资源清理实现。
        """
        await _await_maybe(self.context.dispose())
        self._fibers.clear()
        self._entries.clear()
        self._manifests.clear()
        self._errors.clear()
        self._started_at.clear()

    def _missing(self, fiber: Fiber | None) -> tuple[str, ...]:
        """根据官方 Fiber 声明推导尚未提供的 Service key。

        这只是诊断查询，不会主动 ``provide``、重试或改变 Fiber 状态。真正的依赖
        激活仍由 Cordis 的依赖机制完成。
        """
        if fiber is None:
            return ()
        missing: list[str] = []
        for name in fiber.inject:
            if fiber.ctx.get(name, strict=False) is None:
                missing.append(str(name))
        return tuple(missing)

    def statuses(self) -> tuple[PluginStatus, ...]:
        """把官方 Fiber 状态投影成稳定的 ftre 诊断快照。

        ``fiber is None`` 通常表示入口导入或配置验证阶段就失败；有 Fiber 但状态
        FAILED 则表示 apply/Fiber 生命周期失败。通过 error_code 区分两类，客户端
        和启动日志不必解析异常文本。
        """
        result: list[PluginStatus] = []
        for plugin_id, manifest in self._manifests.items():
            fiber = self._fibers.get(plugin_id)
            error = self._errors.get(plugin_id)
            state: FiberState | str = fiber.state if fiber else FiberState.FAILED
            if error is None and state is FiberState.FAILED:
                error_code = "apply_failed"
            elif error is not None:
                error_code = "entry_import_failed" if fiber is None else "apply_failed"
            else:
                error_code = None
            result.append(
                PluginStatus(
                    id=plugin_id,
                    source=manifest.source,
                    entry=manifest.entry_text,
                    state=state,
                    required=manifest.required,
                    error=str(error) if error else None,
                    error_code=error_code,
                    missing=self._missing(fiber),
                    restart_required=self.restart_required,
                    duration_ms=(time.perf_counter() - self._started_at[plugin_id]) * 1000,
                )
            )
        return tuple(result)
