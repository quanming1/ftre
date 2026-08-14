"""
kernel.events — 事件总线（EventHub）

对齐 Cordis EventsService 的事件分发机制，Python 化实现。插件之间不直接
互相调用，而是通过事件协作；监听器随所属插件的生命周期注册与撤销。

分发模式（DispatchMode）：
- emit      同步广播，逐个调用监听器，忽略返回值（async 监听器 fire-and-forget）
- parallel  并发 await 所有监听器，聚合异常
- serial    串行 await 监听器，遇到首个"终止值"（非 None/False）即短路返回
- bail      同步版 serial，首个终止值短路（仅支持同步监听器）
- waterfall 中间件链：每个监听器收到 (args..., next)，调 next() 传递下游，
            不调 next() 即否决后续；返回最外层监听器的结果
- filter    reduce 风格（ftre 特有，兼容现有 hook chain）：逐个调用监听器，
            每个的返回值作为下一个的输入，返回最终值。

监听器异常一律捕获并记录日志，不拖垮分发主流程（插件出错不应影响其他插件）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

if False:  # pragma: no cover - typing-only imports without runtime cycles
    from ftre_agent_core.tool import ToolRegistry

    from ftre.agent.agent_manager import AgentProfile
    from ftre.config import AgentConfig

logger = logging.getLogger(__name__)

# 监听器：同步或异步，接收任意参数
Listener = Callable[..., Any | Awaitable[Any]]
# on/once 返回的资源释放函数；调用后移除监听器，若当时仍在注册状态返回 True
Disposer = Callable[[], bool]

__all__ = [
    "AGENT_BEFORE_MESSAGES_BUILD",
    "AGENT_BEFORE_RUN",
    "INTERNAL_PLUGIN_STATUS",
    "AgentRunContext",
    "Disposer",
    "EventHub",
    "Listener",
    "MessagesBuildContext",
    "append_to_first_system",
    "is_bailed",
]


AGENT_BEFORE_MESSAGES_BUILD = "agent/before_messages_build"
AGENT_BEFORE_RUN = "agent/before_run"
INTERNAL_PLUGIN_STATUS = "internal/plugin_status"


def append_to_first_system(messages: list[dict], text: str) -> None:
    """Append text to the first system message, creating one when necessary."""
    text = (text or "").strip()
    if not text:
        return
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            current = (message.get("content") or "").rstrip()
            message["content"] = f"{current}\n\n{text}" if current else text
            return
    messages.insert(0, {"role": "system", "content": text})


@dataclass
class MessagesBuildContext:
    """Mutable input for the ``agent/before_messages_build`` filter event."""

    session_id: str
    channel_id: str
    inbound_data: dict
    workspace: str
    reply_id: str = ""
    agent_dir: str = ""
    event_loop: Any = None
    config: AgentConfig = None
    messages: list = field(default_factory=list)


@dataclass
class AgentRunContext:
    """Mutable input for the ``agent/before_run`` filter event."""

    session_id: str
    channel_id: str
    messages: list[dict]
    config: AgentConfig
    agent_profile: AgentProfile | None = None
    agent_tool_registry: ToolRegistry | None = None
    workspace: str = ""


def is_bailed(value: Any) -> bool:
    """判断一个返回值是否为"终止值"（serial/bail 的短路条件）。

    与 Cordis 一致：非 None、非 False、非 undefined（Python 无 undefined）即为终止。
    """
    return value is not None and value is not False


class EventHub:
    """事件总线：注册监听器 + 按模式分发事件。

    一个 EventHub 实例对应一个 FtreContext（根上下文或子上下文共享同一实例）。
    监听器按注册顺序存储；`prepend=True` 时插入到最前。
    """

    def __init__(self) -> None:
        # event name -> 监听器列表（按注册顺序）
        self._hooks: dict[str, list[Listener]] = {}

    # ── 注册 ─────────────────────────────────────────────────────

    def on(self, name: str, listener: Listener, *, prepend: bool = False) -> Disposer:
        """注册一个事件监听器，返回用于移除它的 disposer。

        Args:
            name: 事件名称。
            listener: 同步或异步回调，接收分发参数。
            prepend: True 时插入到已有监听器之前（默认追加到末尾）。

        Returns:
            disposer：调用后移除该监听器；若调用时监听器仍注册则返回 True。
        """
        hooks = self._hooks.setdefault(name, [])
        if prepend:
            hooks.insert(0, listener)
        else:
            hooks.append(listener)

        def dispose() -> bool:
            try:
                hooks.remove(listener)
                return True
            except ValueError:
                return False

        return dispose

    def once(self, name: str, listener: Listener, *, prepend: bool = False) -> Disposer:
        """注册一个"仅触发一次"的监听器，首次调用后自行注销。"""
        disposer_holder: list[Disposer] = []

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if disposer_holder:
                disposer_holder[0]()
            return listener(*args, **kwargs)

        disposer = self.on(name, wrapper, prepend=prepend)
        disposer_holder.append(disposer)
        return disposer

    # ── 分发 ─────────────────────────────────────────────────────

    def emit(self, name: str, *args: Any, **kwargs: Any) -> None:
        """同步广播：逐个调用监听器，忽略返回值。

        同步监听器直接调用；异步监听器以 fire-and-forget 方式调度
        （需要正在运行的事件循环，否则记录警告并跳过）。
        """
        for listener in list(self._hooks.get(name, ())):
            try:
                result = listener(*args, **kwargs)
                if inspect.isawaitable(result):
                    self._schedule_fire_and_forget(name, result)
            except Exception:
                logger.exception("[events] emit 监听器异常已跳过: event=%s", name)

    async def parallel(self, name: str, *args: Any, **kwargs: Any) -> None:
        """并发分发：同时 await 所有监听器，全部完成后返回；聚合异常。"""
        listeners = list(self._hooks.get(name, ()))
        if not listeners:
            return
        results = await asyncio.gather(
            *(self._invoke(listener, *args, **kwargs) for listener in listeners),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            for err in errors:
                logger.error(
                    "[events] parallel 监听器异常: event=%s", name, exc_info=err
                )

    async def serial(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """串行分发：依次 await 监听器，遇到首个终止值即短路返回该值。"""
        for listener in list(self._hooks.get(name, ())):
            try:
                result = await self._invoke(listener, *args, **kwargs)
            except Exception:
                logger.exception("[events] serial 监听器异常已跳过: event=%s", name)
                continue
            if is_bailed(result):
                return result
        return None

    def bail(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """同步短路分发：依次调用监听器，遇到首个终止值即返回。

        异步监听器（协程函数，或返回 awaitable 的同步函数）在此模式下不被
        等待，会被跳过并记录警告；返回的协程会被关闭以避免 RuntimeWarning。
        """
        for listener in list(self._hooks.get(name, ())):
            try:
                result = listener(*args, **kwargs)
            except Exception:
                logger.exception("[events] bail 监听器异常已跳过: event=%s", name)
                continue
            if inspect.isawaitable(result):
                if hasattr(result, "close"):
                    result.close()
                logger.warning("[events] bail 跳过异步监听器: event=%s", name)
                continue
            if is_bailed(result):
                return result
        return None

    async def waterfall(
        self, name: str, *args: Any, inner: Callable[..., Any] | None = None
    ) -> Any:
        """中间件链分发：每个监听器收到 ``(*args, next)``。

        监听器调用 ``next()`` 会把控制权交给下游监听器（最终交给 ``inner``）；
        不调用 ``next()`` 直接返回即否决后续执行。返回最外层监听器的结果。

        Args:
            name: 事件名称。
            args: 传递给监听器的参数（next 由框架追加为最后一个参数）。
            inner: 最内层默认行为；无监听器或全部委托到底时调用。
        """
        listeners = list(self._hooks.get(name, ()))

        async def build_chain(index: int) -> Any:
            if index >= len(listeners):
                if inner is None:
                    return None
                return await self._invoke(inner, *args)
            listener = listeners[index]

            async def next() -> Any:
                return await build_chain(index + 1)

            return await self._invoke(listener, *args, next)

        return await build_chain(0)

    async def filter(self, name: str, value: Any) -> Any:
        """reduce 风格分发（兼容现有 hook chain）：逐个调用监听器，
        每个的返回值作为下一个的输入，返回最终值。

        监听器返回 None 视为"未改写"，沿用当前值。
        监听器抛异常被捕获并跳过（用当前值继续）。
        """
        current = value
        for listener in list(self._hooks.get(name, ())):
            try:
                result = await self._invoke(listener, current)
            except Exception:
                logger.exception("[events] filter 监听器异常已跳过: event=%s", name)
                continue
            if result is not None:
                current = result
        return current

    # ── 内部 ─────────────────────────────────────────────────────

    @staticmethod
    async def _invoke(listener: Listener, *args: Any, **kwargs: Any) -> Any:
        """调用监听器，自适应同步/异步。"""
        result = listener(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _schedule_fire_and_forget(name: str, coro: Awaitable[Any]) -> None:
        """把异步监听器结果以 fire-and-forget 方式调度到当前事件循环。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "[events] emit 遇异步监听器但无运行中的事件循环，已丢弃: event=%s", name
            )
            coro.close()  # type: ignore[attr-defined]
            return

        async def _run() -> None:
            try:
                await coro
            except Exception:
                logger.exception("[events] emit 异步监听器异常: event=%s", name)

        loop.create_task(_run())
