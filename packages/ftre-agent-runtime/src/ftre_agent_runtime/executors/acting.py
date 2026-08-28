"""Acting 执行器 + Exit 执行器。

本模块实现 ReAct 状态机中两个“动作执行器”：

ActingExecutor：
  负责 Acting 动作的执行——并发跑工具、成组写入 Memory、产出流事件。
  它是工具调用的“编排层”：决定 spawn 顺序、写 memory 的顺序、事件的产出顺序，
  以及取消信号的传播。真正的工具执行由 ToolService 负责。

ExitExecutor：
  负责 Exit 动作的执行——在 Agent 准备结束回复时，先过一遍 stop-decision Hook，
  根据返回值决定是真的退出还是注入续写提示继续下一轮；真正退出时设置终态并
  产出 ReplyEndEvent。
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from ftre_agent.event import (
    AgentStreamEvent,
    HintBlockEvent,
    ReplyEndEvent,
    RequireUserConfirmEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
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
from ftre_agent.types import ReplyFinishedReason
from ftre_llm import ToolCall

from ..message_context import MessageContext
from ..run_state import Acting, CancelledError, Exit, ExitOutcome, RunStatus
from ..tool_calls import ToolCallScheduler

if TYPE_CHECKING:
    from ..run_state import RunState


# ═══════════════════════════════════════════════════════════════
# ActingExecutor
# ═══════════════════════════════════════════════════════════════

class ActingExecutor:
    """执行 Acting 动作：并发跑工具、成组写入 Memory、产出流事件。

    职责定位（与 ToolCallScheduler 的分工）：
      - 本类是“编排层”：拿到 Reasoning 已写入 context 的 tool_calls 后，负责
        权限分流、调度工具并把 tool(result) 追加到当前 message_id 对应的
        AssistantMsg，向上层
        yield 工具结果与 hint 事件，并在取消时抛出 CancelledError。
      - ToolCallScheduler 只负责单个调用的并发、取消和结果顺序；ToolService 负责
        Tool Hook、权限、审批、注入、执行和归一化。
      本类不直接 await 单个工具，而是通过调度器间接驱动。
    """

    def __init__(
        self,
        agent,
        state: RunState,
        tool_scheduler: ToolCallScheduler,
    ):
        """初始化 Acting 执行器。

        参数：
          - agent: Runtime Agent 实例，提供消息上下文和 ToolView。
          - state: 当前 run() 的运行状态（RunState），提供 reply_id、trace_span、
            is_cancelled 等运行期上下文。
          - tool_scheduler: ToolCallScheduler，负责 spawn / gather_results，
            通常在整个 agent 运行期共享一个实例。
        """
        self.agent = agent
        self.state = state
        self.tool_scheduler = tool_scheduler

    async def stream(self, action: Acting) -> AsyncGenerator[AgentStreamEvent, None]:
        """执行一轮工具调用；按权限决策决定「整批执行」还是「整批挂起」。

        权限分流（A1 整批语义）：
          1) 本轮全部 tool_call 都被判定 ALLOW（空规则 + default ALLOW 即不拦截）
             → Reasoning 已保存完整模型响应，再走 spawn → gather → 写 tool 结果，
             行为与无权限配置时完全一致；
          2) 只要存在任一 ASK 或 DENY → 整批挂起，谁都不执行：
             把已经保存的 ASK tool_call 状态置 ASKING，
             为每个 ASK 逐个 yield RequireUserConfirmEvent，然后 return。
             DENY 的调用此时不写结果（甲方案：一起等，恢复时整批处理才写 DENIED）。

        暂停信号：本方法通过 yield RequireUserConfirmEvent 表达挂起，
        react_runner._loop 收到该事件即知道本轮挂起，停止循环但不 finalize。
        """
        tool_calls = action.tool_calls

        async for event in self._execute_calls(tool_calls):
            yield event
        return

    async def resume_execute(self) -> AsyncGenerator[AgentStreamEvent, None]:
        """恢复阶段：从 context 重建待收尾的 tool_call 并统一处理。

        待收尾 = context 里尚无配对 tool_result 的 ToolCallBlock。它们的状态已由
        react_runner 依据用户确认更新：
          - ALLOWED           → 执行工具，写 SUCCESS/ERROR 结果；
          - FINISHED（被拒绝）→ 不执行，写 DENIED 结果；
          - PENDING（DENY）   → 不执行，写 DENIED 结果。

        不接收 action 参数——待执行清单完全从持久化的 context 重建，因此进程重启后
        加载 state.json 也能恢复，不依赖任何实例内存。
        完整 assistant 响应在挂起时已写过，这里不再重复写。
        """
        pending_blocks = self._pending_tool_calls_from_context()
        # ToolCallBlock → ToolCall（执行所需）
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.arguments or {})
            for b in pending_blocks
        ]
        allowed_ids = {
            b.id for b in pending_blocks if b.state == ToolCallState.ALLOWED
        }
        allowed = [tc for tc in tool_calls if tc.id in allowed_ids]
        denied = [tc for tc in tool_calls if tc.id not in allowed_ids]

        # 已确认放行的调用：走原执行流程，但跳过 assistant 消息（挂起时已写）
        if allowed:
            async for event in self._execute_calls(allowed):
                yield event

        # 被拒绝（用户拒绝）或被 DENY 的调用：写 DENIED 结果 + 产出事件三元组
        reply_id = self.state.reply_id
        message_id = self.state.message_id or self.state.reply_id
        for tc in denied:
            denied_text = f"[USER_DENIED] 用户拒绝了工具 [{tc.name}] 的执行"
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
            yield ToolResultStartEvent(
                reply_id=reply_id, tool_call_id=tc.id, tool_call_name=tc.name,
            )
            # 与内存中的 ToolResultBlock 保持一致，让宿主投影能够把拒绝原因
            # 写入持久化快照；否则下次恢复时 provider 只能收到空 tool content。
            yield ToolResultTextDeltaEvent(
                reply_id=reply_id,
                tool_call_id=tc.id,
                delta=denied_text,
            )
            yield ToolResultEndEvent(
                reply_id=reply_id, tool_call_id=tc.id,
                state=ToolResultState.DENIED, metadata={},
            )

    def _pending_tool_calls_from_context(self) -> list[ToolCallBlock]:
        """从 context 重建"待收尾"的 ToolCallBlock 列表（保持出现顺序）。

        待收尾 = 尚无配对 tool_result 的 tool_call。已经写过结果的 tool_call
        （其 id 出现在某个 ToolResultBlock 上）视为已完成，跳过。这样恢复清单
        完全由持久化 context 推导，不依赖任何实例内存或传入参数。
        """
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
    ) -> AsyncGenerator[AgentStreamEvent, None]:
        """并发执行一批 tool_call 并成组写入结果、产出事件（原 stream 的执行逻辑）。

        完整 assistant 响应由 ReasoningExecutor 写入；恢复执行时则已经存在于
        持久化 context。本方法只追加工具结果，不创建 assistant 消息。

        整体流程分四个阶段：
          1) spawn：为每个 tool_call 创建并发任务（不 await，立即返回 Task）；
          2) gather_results：阻塞等待全部完成，归一化异常与取消，按原序拿回结果；
          3) 逐条写 tool(result)；
          4) 延后追加 pending_hints 并 yield 对应事件；若发生取消则抛出
             CancelledError。
        """
        reply_id = self.state.reply_id
        message_id = self.state.message_id or self.state.reply_id

        # ── 阶段 1：spawn 所有工具任务 ──
        # 对每个 tool_call 调 tool_scheduler.spawn 创建 asyncio.Task，这里【不 await】——
        # spawn 只是把任务丢进事件循环并立即返回 Task 句柄。之所以不在循环里等待，
        # 是为了让所有工具任务能并发跑起来（一个慢工具不阻塞其它工具的启动），
        # 同时保持本循环纯同步、不阻塞对 LLM 流的继续消费。真正的等待在阶段 2 完成。
        tool_tasks: dict[str, asyncio.Task] = {}
        for call in tool_calls:
            tool_tasks[call.id] = self.tool_scheduler.spawn(
                call,
                self.state,
                parent_span=self.state.trace_span,
            )

        # ── 阶段 2：等待全部完成 + 取消处理 ──
        # gather_results 会 await 所有任务结束（return_exceptions=True 收敛异常），
        # 把 CancelledError / Exception 归一化成 cancelled / failed 的 ToolResult，
        # 并返回 cancelled 标志。这一步阻塞直到所有工具都有结果（含失败/取消）。
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
                yield RequireUserConfirmEvent(
                    reply_id=reply_id,
                    tool_call_id=call.id,
                    tool_call_name=call.name,
                    arguments=call.input or {},
                    reason=result.metadata.get("reason", ""),
                    rule_id=result.metadata.get("rule_id"),
                )
            return

        # ── 阶段 3：成组写入 tool results ──
        # 对应的 assistant(tool_calls) 已由 ReasoningExecutor 写入，或在确认恢复时
        # 从持久化 context 还原。这里只连续追加结果，维持工具协议的相邻约束。
        # 工具可能返回事件对象（ToolResult.event，如 HintBlockEvent）而非纯文本，
        # 这些事件需要向上冒泡给调用方。但它们不能在下面的循环里立即写入 memory，
        # 否则会被插在 tool(result) 序列中间。先收集起来，待结果全部写完再处理。
        pending_hints: list[AgentStreamEvent] = []

        for tc, result in zip(tool_calls, results):
            # 执行成功的调用状态置 FINISHED，与写入的结果配对。
            MessageContext.set_tool_call_state(
                self.agent.state.context, tc.id, ToolCallState.FINISHED
            )
            # 写入这一条 tool 结果（role="tool"），与上面的 assistant 消息配对
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
            )

            # 产出工具结果事件三元组：Start → (TextDelta) → End
            yield ToolResultStartEvent(
                reply_id=reply_id, tool_call_id=tc.id, tool_call_name=tc.name,
            )
            if result.result:
                yield ToolResultTextDeltaEvent(
                    reply_id=reply_id, tool_call_id=tc.id, delta=result.result,
                )
            state = ToolResultState.SUCCESS if not result.error else ToolResultState.ERROR
            yield ToolResultEndEvent(
                reply_id=reply_id, tool_call_id=tc.id,
                state=state, metadata=result.metadata or {},
            )

            # 工具返回了事件对象（非文本）→ 暂存，阶段 4 统一处理
            if result.event is not None:
                pending_hints.append(result.event)

        # ── 阶段 4：延后追加 pending_hints ──
        # 之所以把 hints 放到所有 tool(result) 之后再写入，而不是在循环里即时写：
        # hint 本质是 role="user" 的注入消息，若把它插在某两条 tool(result) 之间，
        # 会打破“同组 tool 结果必须连续”的消息序列合法性，后续 LLM 调用会因消息
        # 顺序非法而报错。因此必须等全部 tool 结果写完，再统一把 hints 追加到末尾。
        for ev in pending_hints:
            if isinstance(ev, HintBlockEvent):
                content = ev.hint if isinstance(ev.hint, str) else str(ev.hint)
            else:
                content = str(ev)
            hint_block = HintBlock(
                id=uuid.uuid4().hex[:16],
                source="tool",
                hint=content,
            )
            MessageContext.append_reply_blocks(
                self.agent.state.context,
                message_id,
                [hint_block],
            )
            yield HintBlockEvent(
                reply_id=reply_id,
                block_id=hint_block.id,
                source="tool",
                hint=content,
            )

        # ── 取消传播 ──
        # cancelled=True 时抛出内部 CancelledError（与 asyncio.CancelledError 区分）。
        # 传播路径：本方法抛出 → react_runner._loop 的 except CancelledError 捕获 →
        # 调 react_runner._finalize(INTERRUPTED) 设置终态 → yield ReplyEndEvent(INTERRUPTED)。
        # 注意：此时 memory 与事件已写完，取消只影响“本轮之后是否继续 / 以何终态收尾”。
        if cancelled:
            raise CancelledError()




# ═══════════════════════════════════════════════════════════════
# ExitExecutor
# ═══════════════════════════════════════════════════════════════

class ExitExecutor:
    """执行 Exit 动作：stop-decision 检查 + 产出 ReplyEnd + 设置终态。

    stop-decision Hook 的两种路径：
      - block：Hook 决定不让 Agent 停下。此时不产出 ReplyEndEvent、不设终态，
        而是把 Hook 的 reason 作为续写提示注入 memory、yield 一个 HintBlockEvent，
        并把 outcome 置为 should_continue=True，让 react_runner._loop 进入下一轮迭代。
      - allow（或无 Hook / 非 COMPLETED 退出）：正常退出路径——_finalize 设置终态，
        yield ReplyEndEvent，outcome 保持 should_continue=False，主循环收到后 return。

    仅当 finished_reason == COMPLETED 时才触发 stop-decision：ERROR / EXCEED_MAX_ITERS 属于
    异常 / 超限退出，没有“要不要让 Agent 继续”的语义，不需要问 Hook，直接走正常退出。
    """

    def __init__(
        self,
        agent,
        state: RunState,
        hooks: HookDispatcher | None = None,
        hook_context: object | None = None,
    ):
        """初始化 Exit 执行器。

        参数：
          - agent: 宿主 Agent 实例，提供 memory（注入续写提示）等。
          - state: 当前 run() 的运行状态（RunState），退出时由 _finalize 写入终态。
          - hooks: 宿主注入的 Dispatcher；None 时使用 StopTurn 默认行为。
          - hook_context: 宿主 scope carrier。

        实例属性：
          - outcome: 本次 Exit 的结果载体（ExitOutcome），初始 should_continue=False。
            ContinueTurn 时被置为 should_continue=True，供 react_runner._loop 判断
            是否继续下一轮迭代。
        """
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

    async def stream(self, action: Exit) -> AsyncGenerator[AgentStreamEvent, None]:
        """执行退出逻辑：先过 stop-decision Hook，再决定续写还是真正退出。

        - finished_reason == COMPLETED：触发 stop-decision，Hook 可 Continue（续写）或 Stop（退出）。
        - 其它 reason（ERROR / EXCEED_MAX_ITERS / INTERRUPTED）：跳过 Hook，直接退出。
        """
        session_id = self.state.runtime_context.get("session_id", "")
        reply_id = self.state.reply_id
        message_id = self.state.message_id or self.state.reply_id

        # ── 仅 COMPLETED 触发 stop-decision ──
        # ERROR（LLM 调用失败 / 空响应耗尽重试）和 EXCEED_MAX_ITERS（超过最大迭代数）
        # 都是“被迫退出”，没有“让 Hook 决定是否继续”的语义——Hook 没法把一个失败的
        # LLM 调用变成成功，也无法突破迭代上限。故只在 Agent 主动完成（COMPLETED）时，
        # 给 Hook 一次“拦下并要求继续干活”的机会。
        if action.finished_reason == ReplyFinishedReason.COMPLETED:
            stop_output = await self._dispatch_stop()

            # ── ContinueTurn：不退出，注入续写提示 ──
            # Hook 返回 decision="block" 表示“别停，接着干”。行为：
            #   1) 把 Hook 的 reason（或默认“继续工作。”）作为当前 reply 的 HintBlock
            #      写入 memory；Provider 边界会把它切成 user 消息供下一轮读取；
            #   2) yield 一个 HintBlockEvent（hide=True / internal=True），用于向上层 / 前端
            #      传达“本次停止被 Hook 拦截”，但标记为内部隐藏事件，不直接展示给用户；
            #   3) 置 outcome.should_continue=True，react_runner._loop 据此不 return 而是
            #      continue 进入下一轮迭代；本方法提前 return，不产出 ReplyEndEvent、不设终态。
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
                    yield HintBlockEvent(
                        reply_id=reply_id,
                        block_id=hint_block.id,
                        source="system",
                        hint=hint,
                        metadata={"hide": True, "internal": True, "reason": "turn_stopping_continue"},
                    )
                    self.outcome = ExitOutcome(should_continue=True, continue_hint=hint)
                    return

        # ── 正常退出路径 ──
        # 走到这里说明：非 COMPLETED 退出，或 COMPLETED 但 StopTurn / 无 Hook。
        # 1) _finalize 把 reason / error / error_code 写入 RunState，并把 status 映射成
        #    COMPLETED / ERROR / CANCELLED 终态；
        # 2) yield ReplyEndEvent 通知上层本轮回复正式结束；
        # 3) outcome 保持默认（should_continue=False），react_runner._loop 收到后 return。
        self._finalize(action.finished_reason, action.error, action.error_code)

        yield ReplyEndEvent(
            session_id=session_id,
            reply_id=reply_id,
            finished_reason=action.finished_reason,
            error={"message": action.error, "code": action.error_code} if action.error else None,
        )
        self.outcome = ExitOutcome()

    def _finalize(self, reason: ReplyFinishedReason, error: str | None, error_code: str | None) -> None:
        """把退出原因与错误信息写入 RunState，并设置终态。

        reason → RunStatus 映射：
          - INTERRUPTED → CANCELLED（被取消 / 中断）
          - ERROR       → ERROR（LLM 调用失败或空响应耗尽重试）
          - 其它（COMPLETED / EXCEED_MAX_ITERS）→ COMPLETED

        EXCEED_MAX_ITERS 归入 COMPLETED 而非 ERROR：它不是错误，而是达到迭代上限的正常停止。
        error / error_code 仅在非空时写入，供上层 tracing 与 ReplyEndEvent 使用。
        """
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
