"""ReAct ToolCall 并发调度器。

它只负责按模型顺序启动、等待和取消 ToolView 调用；权限、审批、注入、执行
和结果归一化全部由 Host ToolService 完成。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ftre_agent.event import EventBase
from ftre_agent.tool import ToolContext, ToolExecutionResult
from ftre_llm import ToolCall


@dataclass
class ToolResult:
    call_id: str
    name: str
    result: str
    error: str | None = None
    status: str = "completed"
    metadata: dict = field(default_factory=dict)
    event: EventBase | None = None


class ToolCallScheduler:
    """ToolView 调度器，不持有 Registry 或工具实现。"""

    def __init__(self, tool_view, *, hooks=None, hook_context=None) -> None:
        self.tool_view = tool_view
        self.hooks = hooks
        self.hook_context = hook_context

    @staticmethod
    def _cancellation(state):
        value = state.runtime_context.get("cancellation")
        return value if isinstance(value, asyncio.Event) else asyncio.Event()

    async def run_one(self, call_id, name, arguments, state, parse_failed=False):
        if parse_failed:
            return ToolResult(
                call_id,
                name,
                "[PARSE_ERROR] Tool call arguments were malformed JSON.",
                error="malformed JSON arguments",
                status="failed",
            )
        cancellation = self._cancellation(state)
        context = ToolContext(
            call_id=call_id,
            name=name,
            arguments=arguments,
            metadata={
                **dict(state.runtime_context),
                "hook_context": self.hook_context,
                "iteration": state.iteration,
            },
            cancellation=cancellation,
        )
        try:
            execution = await self.tool_view.execute(name, arguments, context)
            if not isinstance(execution, ToolExecutionResult):
                raise TypeError("ToolView.execute must return ToolExecutionResult")
            if execution.status == "cancelled":
                return ToolResult(
                    call_id,
                    name,
                    execution.output,
                    execution.error,
                    "cancelled",
                    dict(execution.metadata),
                )
            if execution.status == "failed":
                return ToolResult(
                    call_id,
                    name,
                    execution.output or execution.error or "Tool failed",
                    execution.error,
                    "failed",
                    dict(execution.metadata),
                    execution.value if isinstance(execution.value, EventBase) else None,
                )
            return ToolResult(
                call_id,
                name,
                execution.output,
                execution.error,
                execution.status,
                dict(execution.metadata),
                execution.value if isinstance(execution.value, EventBase) else None,
            )
        except asyncio.CancelledError:
            return ToolResult(
                call_id,
                name,
                "[CANCELLED] Tool execution was cancelled.",
                status="cancelled",
            )
        except Exception as exc:  # noqa: BLE001 - normalize at tool boundary
            return ToolResult(call_id, name, str(exc), error=str(exc), status="failed")

    def spawn(self, call: ToolCall, state, parent_span=None) -> asyncio.Task:
        del parent_span
        return asyncio.create_task(
            self.run_one(
                call.id,
                call.name,
                call.input if call.input is not None else {},
                state,
                parse_failed=call.input is None,
            ),
            name=f"tool-{call.id}",
        )

    async def gather_results(self, tool_calls, tasks, state):
        externally_cancelled = False
        try:
            raw = await asyncio.gather(*tasks.values(), return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks.values():
                task.cancel()
            raw = await asyncio.gather(*tasks.values(), return_exceptions=True)
            externally_cancelled = True
        finished = {}
        for call_id, item in zip(tasks, raw):
            if isinstance(item, BaseException):
                finished[call_id] = ToolResult(
                    call_id,
                    next((call.name for call in tool_calls if call.id == call_id), call_id),
                    "[INTERRUPTED] Tool execution was interrupted.",
                    status="cancelled" if externally_cancelled else "failed",
                )
            else:
                finished[call_id] = item
        results = [
            finished.get(call.id)
            or ToolResult(call.id, call.name, "[INTERRUPTED] Tool result was lost.", status="failed")
            for call in tool_calls
        ]
        cancelled = externally_cancelled or any(item.status == "cancelled" for item in results)
        return results, cancelled


__all__ = ["ToolCallScheduler", "ToolResult"]
