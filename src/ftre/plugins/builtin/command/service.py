"""Command Service：接入层指令的解析、注册和执行边界。

Command 是接入协议，不是 Agent 的聊天消息。Service 把 slash command 解析成
结构化定义并交给 ``CommandRuntime``；普通命令在控制面完成，不进入
LLM 上下文，Feature 只需要依赖这个公开 key。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from .manager import CommandRuntime
from .types import CommandDef, CommandResult, Handler

logger = logging.getLogger(__name__)


class CommandService:
    """Command Plane 的公开 Service；不暴露内部注册表或 Agent runtime。"""

    key = "commands"

    def __init__(
        self,
        runtime: CommandRuntime | None = None,
        *,
        lifecycle=None,
    ) -> None:
        self.runtime = runtime or CommandRuntime(lifecycle=lifecycle)
        # Command 的接纳与执行是两个不同的边界：消息总线只需要知道命令已被
        # 接受，耗时 handler（例如压缩）必须在自己的 Task 中运行，不能阻塞
        # 全局 inbound consumer。这里保存 Task 是为了让 Plugin unload 时可以
        # 取消并排空正在运行的命令，而不是把后台任务遗留到下一次 Composition。
        self._background_tasks: set[asyncio.Task] = set()
        self._inflight: dict[str, asyncio.Task] = {}
        self._completed: set[str] = set()

    def register(self, command: str, handler: Handler, **kwargs: Any) -> Callable[[], bool]:
        """注册一个命令并返回可逆 disposer，供 Provider 绑定到 Fiber。"""
        return self.runtime.register(command, handler, **kwargs)

    def register_def(self, command_def: CommandDef) -> Callable[[], bool]:
        """注册已构造的命令定义并返回可逆 disposer。"""
        return self.runtime.register_def(command_def)

    def parse(self, data: Any) -> CommandDef | None:
        """从接入数据解析 slash command 定义，不执行它。"""
        return self.runtime.parse(data)

    def is_command_input(self, data: Any) -> bool:
        """Return whether an inbound user message is slash-command shaped."""
        return self.runtime.text_from(data) is not None

    def match(self, data: Any) -> CommandDef | None:
        """匹配普通用户命令；未命中返回 None。"""
        return self.runtime.match(data)

    def match_any(self, data: Any) -> CommandDef | None:
        """匹配普通或 system 命令，供内部控制面使用。"""
        return self.runtime.match_any(data)

    async def dispatch_inbound(
        self,
        inbound: Any,
        *,
        system: bool = False,
        definition: CommandDef | None = None,
    ) -> CommandResult | None:
        """在接入边界执行一个结构化命令并返回统一结果。"""
        return await self.runtime.dispatch_inbound(
            inbound,
            system=system,
            definition=definition,
        )

    def submit_inbound(
        self,
        inbound: Any,
        *,
        system: bool = False,
        definition: CommandDef | None = None,
        on_result: Callable[[CommandResult], Any] | None = None,
    ) -> bool:
        """异步提交已解析命令，并立即返回接纳结果。

        ``dispatch_inbound`` 保留给需要等待命令结果的内部调用；WebSocket/Bus
        接入必须使用本方法。若沿用 ``await dispatch_inbound``，一个慢命令会占住
        MessageBus 的单一 inbound 消费者，使其它用户消息长时间停在“队列中”，
        甚至在客户端刷新时丢失本地 optimistic 状态。

        返回 ``False`` 只表示没有可执行定义；同一 request_id 已在执行或已成功
        完成时返回 ``True``，但不会重复执行，保证断线重试仍然幂等。
        """
        if definition is None:
            return False
        request_id = str(getattr(inbound.metadata, "request_id", "") or "")
        if request_id and (request_id in self._inflight or request_id in self._completed):
            return True

        task = asyncio.create_task(
            self._run_submitted(
                inbound,
                system=system,
                definition=definition,
                on_result=on_result,
            ),
            name=f"command:{definition.command}:{request_id or 'anonymous'}",
        )
        self._background_tasks.add(task)
        if request_id:
            self._inflight[request_id] = task

        def on_done(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            if request_id and self._inflight.get(request_id) is done:
                self._inflight.pop(request_id, None)
            # _run_submitted converts handler failures into CommandResult；这里仅
            # 兜底消费取消/编程错误，避免“Task exception was never retrieved”。
            if not done.cancelled():
                try:
                    done.exception()
                except Exception:  # pragma: no cover - asyncio callback 兜底
                    logger.exception("[command] background task inspection failed")

        task.add_done_callback(on_done)
        return True

    async def _run_submitted(
        self,
        inbound: Any,
        *,
        system: bool,
        definition: CommandDef,
        on_result: Callable[[CommandResult], Any] | None,
    ) -> None:
        request_id = str(getattr(inbound.metadata, "request_id", "") or "")
        try:
            result = await self.dispatch_inbound(
                inbound,
                system=system,
                definition=definition,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # handler 已写入 command/done error 事件
            logger.exception(
                "[command] background execution failed command=%s request=%s",
                definition.command,
                request_id,
            )
            result = CommandResult.error(f"命令执行失败：{exc}")
        else:
            if request_id and result is not None and result.kind == "success":
                self._completed.add(request_id)

        if on_result is not None and result is not None:
            callback_result = on_result(result)
            if inspect.isawaitable(callback_result):
                await callback_result

    async def close(self) -> None:
        """取消并排空所有后台命令；由 Command Plugin 的 Fiber 调用。"""
        tasks = tuple(self._background_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._inflight.clear()
        self._completed.clear()

    def list(self) -> list[dict[str, Any]]:
        """返回命令定义摘要，不暴露 handler 对象。"""
        return self.runtime.list_commands()


__all__ = ["CommandService"]
