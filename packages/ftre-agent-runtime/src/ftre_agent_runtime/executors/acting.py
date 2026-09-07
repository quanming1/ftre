"""Acting 执行器 + Exit 执行器 —— 会话事件版。

产出的事件（PRD-F41 附录 A）：
  tool/result-start      工具结果开始流式
  assistant/chunk(kind=tool_result_text)  工具结果文本增量
  tool/result            工具结果定稿（whole-value output + state + metadata）
  approval/asked         权限确认挂起
  hint/message           工具提示 / 续写提示
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from ftre_agent.hooks import (
    AGENT_STOP_DECISION_SPEC,
    ContinueTurn,
    HookDispatcher,
    StopDecisionPayload,
    StopTurn,
)
from ftre_agent.message import (
    HintBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
)
from ftre_agent.session.events import (
    ApprovalAsked,
    ApprovalAskedData,
    AssistantChunk,
    AssistantChunkData,
    HintData,
    HintMessage,
    ToolResultData,
    ToolResultStartData,
)
from ftre_agent.session.events import (
    ToolResult as ToolResultEvent,
)
from ftre_agent.session.events import (
    ToolResultStart as ToolResultStartEvent,
)
from ftre_agent.types import ReplyFinishedReason
from ftre_llm import ToolCall

from ..message_context import MessageContext
from ..run_state import Acting, CancelledError, Exit, ExitOutcome, RunState, RunStatus

# ═══════════════════════════════════════════════════════════════
# ActingExecutor
# ═══════════════════════════════════════════════════════════════

class ActingExecutor:
    """执行 Acting 动作：并发跑工具、成组写入 Memory、产出会话事件。"""

    def __init__(
        self,
        agent,
        state: RunState,
        tool_scheduler,
    ):
        self.agent = agent
        self.state = state
        self.tool_scheduler = tool_scheduler

    async def stream(self, action: Acting) -> AsyncGenerator[Any, None]:
        """执行一轮工具调用；按权限决策决定「整批执行」还是「整批挂起」。"""
        async for event in self._execute_calls(action.tool_calls):
            yield event
        return

    async def resume_execute(self) -> AsyncGenerator[Any, None]:
        """恢复阶段：从 context 重建待收尾的 tool_call 并统一处理。"""
        pending_blocks = self._pending_tool_calls_from_context()
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.arguments or {})
            for b in pending_blocks
        ]
        allowed_ids = {
            b.id for b in pending_blocks if b.state == ToolCallState.ALLOWED
        }
        allowed = [tc for tc in tool_calls if tc.id in allowed_ids]
        denied = [tc for tc in tool_calls if tc.id not in allowed_ids]

        if allowed:
            async for event in self._execute_calls(allowed):
                yield event

        reply_id = self.state.reply_id
        message_id = self.state.message_id or self.state.reply_id
        for tc in denied:
            denied_text = f"[USER_DENIED] 用户拒绝了工具 [{tc.name}] 的执行"
            # 与正常工具路径保持相同的时间线：先宣布结果流开始，
            # 再把拒绝结果写入内存和事件日志。
            yield ToolResultStartEvent(
                data=ToolResultStartData(tool_call_id=tc.id, name=tc.name),
                message_id=message_id,
            )
            MessageContext.add_tool_result(
                self.agent.state.context,
                message_id=message_id,
                tool_call_id=tc.id,
                name=tc.name,
                content=denied_text,
                state=ToolResultState.DENIED,
            )
            MessageContext.set_tool_call_state(
                self.agent.state.context, tc.id, ToolCallState.FINISHED
            )
            yield AssistantChunk(
                data=AssistantChunkData(
                    kind="tool_result_text", delta=denied_text, tool_call_id=tc.id
                ),
                message_id=message_id,
            )
            yield ToolResultEvent(
                data=ToolResultData(
                    tool_call_id=tc.id,
                    name=tc.name,
                    output=[{"type": "text", "text": denied_text}],
                    state="denied",
                    metadata={},
                ),
                message_id=message_id,
            )
        del reply_id

    def _pending_tool_calls_from_context(self) -> list[ToolCallBlock]:
        """从 context 重建"待收尾"的 ToolCallBlock 列表（保持出现顺序）。"""
        resulted_ids = {
            block.id
            for message in self.agent.state.context
            for block in message.content
            if isinstance(block, ToolResultBlock)
        }
        pending: list[ToolCallBlock] = []
        for message in self.agent.state.context:
            for block in message.content:
                if isinstance(block, ToolCallBlock) and block.id not in resulted_ids:
                    pending.append(block)
        return pending

    async def _execute_calls(
        self, tool_calls: list[ToolCall]
    ) -> AsyncGenerator[Any, None]:
        """并发执行一批 tool_call 并成组写入结果、产出事件。"""
        message_id = self.state.message_id or self.state.reply_id

        # ── 阶段 1：spawn 所有工具任务 ──
        tool_tasks: dict[str, asyncio.Task] = {}
        for call in tool_calls:
            tool_tasks[call.id] = self.tool_scheduler.spawn(
                call,
                self.state,
                parent_span=self.state.trace_span,
            )

        # 先发出整批 result-start，再等待工具输出。这样客户端不会看到
        # “结果已经结束才开始”的逆序时间线，同时仍保留工具并发执行。
        for call in tool_calls:
            yield ToolResultStartEvent(
                data=ToolResultStartData(tool_call_id=call.id, name=call.name),
                message_id=message_id,
            )

        # ── 阶段 2：等待全部完成 + 取消处理 ──
        results, cancelled = await self.tool_scheduler.gather_results(
            tool_calls, tool_tasks, self.state,
        )

        approval_results = [
            (call, result)
            for call, result in zip(tool_calls, results)
            if result.metadata.get("approval_required")
        ]
        if approval_results:
            for call, result in approval_results:
                MessageContext.set_tool_call_state(
                    self.agent.state.context, call.id, ToolCallState.ASKING
                )
                yield ApprovalAsked(
                    data=ApprovalAskedData(
                        tool_call_id=call.id,
                        name=call.name,
                        arguments=call.input or {},
                        reason=result.metadata.get("reason", ""),
                        rule_id=result.metadata.get("rule_id"),
                    ),
                    message_id=message_id,
                )
            return

        # ── 阶段 3：成组写入 tool results ──
        pending_hints: list[Any] = []

        for tc, result in zip(tool_calls, results):
            MessageContext.set_tool_call_state(
                self.agent.state.context, tc.id, ToolCallState.FINISHED
            )
            MessageContext.add_tool_result(
                self.agent.state.context,
                message_id=message_id,
                tool_call_id=tc.id,
                name=tc.name,
                content=result.result or f"[{tc.name}] 已完成",
                state=(
                    ToolResultState.SUCCESS
                    if not result.error
                    else ToolResultState.ERROR
                ),
                metadata=result.metadata,
            )

            if result.result:
                yield AssistantChunk(
                    data=AssistantChunkData(
                        kind="tool_result_text",
                        delta=result.result,
                        tool_call_id=tc.id,
                    ),
                    message_id=message_id,
                )
            state = "error" if result.error else "success"
            yield ToolResultEvent(
                data=ToolResultData(
                    tool_call_id=tc.id,
                    name=tc.name,
                    output=[{"type": "text", "text": result.result or ""}],
                    state=state,
                    metadata=result.metadata or {},
                ),
                message_id=message_id,
            )

            if result.event is not None:
                pending_hints.append(result.event)

        # ── 阶段 4：延后追加 pending_hints ──
        # hint 必须等全部 tool 结果写完再追加，保证 tool(result) 序列连续。
        for ev in pending_hints:
            hint_text = (
                ev.hint if isinstance(getattr(ev, "hint", None), str)
                else str(getattr(ev, "hint", "") or ev)
            )
            hint_block = HintBlock(
                id=uuid.uuid4().hex[:16],
                source="tool",
                hint=hint_text,
            )
            MessageContext.append_reply_blocks(
                self.agent.state.context,
                message_id,
                [hint_block],
            )
            yield HintMessage(
                data=HintData(hint=hint_text, source="tool"),
                message_id=message_id,
            )

        # ── 取消传播 ──
        if cancelled:
            raise CancelledError()


# ═══════════════════════════════════════════════════════════════
# ExitExecutor
# ═══════════════════════════════════════════════════════════════

class ExitExecutor:
    """执行 Exit 动作：stop-decision 检查 + 设置终态。

    turn/end 事件由 TurnExecutor 在 Host 侧产出；
    ContinueTurn 的续写提示仍产出 hint/message。
    """

    def __init__(
        self,
        agent,
        state: RunState,
        hooks: HookDispatcher | None = None,
        hook_context: object | None = None,
    ):
        self.agent = agent
        self.state = state
        self.hooks = hooks
        self.hook_context = hook_context
        self.outcome: ExitOutcome = ExitOutcome()

    async def _dispatch_stop(self) -> StopTurn | ContinueTurn:
        cancellation = self.state.runtime_context.get("cancellation")
        if not isinstance(cancellation, asyncio.Event):
            cancellation = asyncio.Event()
        payload = StopDecisionPayload(
            agent=self.state.runtime_context.get("agent_subject", self.agent),
            session_id=str(self.state.runtime_context.get("session_id", "")),
            turn_id=self.state.turn_id,
            status="completed",
            request_id=str(self.state.runtime_context.get("request_id", "")),
            cancellation=cancellation,
            last_assistant_text=str(self.state.runtime_context.get("last_assistant_text", "")),
            finish_reason=str(self.state.runtime_context.get("finish_reason", "")),
            iteration=self.state.iteration,
            continuation_count=max(0, int(self.state.runtime_context.get("continuation_count", 0))),
            max_continuations=max(0, int(self.state.runtime_context.get("max_continuations", 3))),
        )
        if self.hooks is None:
            return StopTurn()
        result = await self.hooks.dispatch(
            AGENT_STOP_DECISION_SPEC,
            payload,
            context=self.hook_context,
        )
        if not isinstance(result, (StopTurn, ContinueTurn)):
            raise TypeError("agent/stop-decision must return StopTurn or ContinueTurn")
        return result

    async def stream(self, action: Exit) -> AsyncGenerator[Any, None]:
        """执行退出逻辑：先过 stop-decision Hook，再决定续写还是真正退出。"""
        message_id = self.state.message_id or self.state.reply_id

        if action.finished_reason == ReplyFinishedReason.COMPLETED:
            stop_output = await self._dispatch_stop()

            if isinstance(stop_output, ContinueTurn):
                cancellation = self.state.runtime_context.get("cancellation")
                continuation_count = int(self.state.runtime_context.get("continuation_count", 0))
                max_continuations = int(self.state.runtime_context.get("max_continuations", 3))
                if not (
                    isinstance(cancellation, asyncio.Event) and cancellation.is_set()
                ) and continuation_count < max_continuations:
                    hint = stop_output.prompt
                    self.state.runtime_context["continuation_count"] = continuation_count + 1
                    hint_block = HintBlock(
                        id=uuid.uuid4().hex[:16],
                        source="system",
                        hint=hint,
                    )
                    MessageContext.append_reply_blocks(
                        self.agent.state.context,
                        message_id,
                        [hint_block],
                    )
                    yield HintMessage(
                        data=HintData(hint=hint, source="system"),
                        message_id=message_id,
                    )
                    self.outcome = ExitOutcome(should_continue=True, continue_hint=hint)
                    return

        self._finalize(action.finished_reason, action.error, action.error_code)
        self.outcome = ExitOutcome()

    def _finalize(self, reason: ReplyFinishedReason, error: str | None, error_code: str | None) -> None:
        """把退出原因与错误信息写入 RunState，并设置终态。"""
        self.state.done_reason = reason
        self.state.status = (
            RunStatus.CANCELLED if reason == ReplyFinishedReason.INTERRUPTED
            else RunStatus.ERROR if reason == ReplyFinishedReason.ERROR
            else RunStatus.COMPLETED
        )
        if error:
            self.state.error = error
        if error_code:
            self.state.error_code = error_code
