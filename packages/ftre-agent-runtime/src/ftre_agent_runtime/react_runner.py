"""ReActRunner — ReAct Agent 的核心执行引擎（状态机重构版）。

整体设计借鉴 AgentScope 的 _next_action() 纯决策函数模式，同时保留
原有的 LLM 重试、空响应恢复、stop-decision Hook、Tracing、工具并发执行、
成组写入 Memory 等生产能力。

架构分三层：

  decide()               纯决策函数，只读状态，返回动作类型
  ReasoningExecutor      执行 Reasoning 动作：调 LLM + 流式 + 重试
  ActingExecutor         执行 Acting 动作：工具并发 + 成组写入 Memory
  ExitExecutor           执行 Exit 动作：stop-decision Hook + 产出 ReplyEnd

主循环 _loop() 只做 match 分发：

  while True:
      action = decide(state, prev)
      match action:
          Reasoning → prev = reasoning_executor.stream(action)
          Acting    → acting_executor.stream(action); prev = None
          Exit      → exit_executor.stream(action); return

取消协议：
  外部调用 cancel_nowait() → Task.cancel() → CancelledError 沿调用栈传播
  → _loop 的 except 捕获 → _finalize(INTERRUPTED) → yield ReplyEnd(INTERRUPTED)
  不引入 CancellationToken，纯依赖 asyncio 协作式取消。

运行锁：
  同一 Agent 禁止并发 run()。run() 开始时记录 asyncio.current_task()，
  重复调用直接抛 RuntimeError。finally 中清除。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from ftre_agent.event import (
    AgentStreamEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    UserConfirmResultEvent,
)
from ftre_agent.hooks import (
    AGENT_BEFORE_REASONING_SPEC,
    BeforeReasoningPayload,
    BeforeReasoningResult,
)
from ftre_agent.message import ToolCallBlock, ToolCallState
from ftre_agent.tracing import RunStatus as TraceRunStatus
from ftre_agent.tracing import RunType
from ftre_agent.types import ReplyFinishedReason
from ftre_llm import LlmAdapter

from .executors.acting import ActingExecutor, ExitExecutor
from .executors.reasoning import ReasoningExecutor
from .message_context import MessageContext
from .run_state import (
    Acting,
    CancelledError,
    Exit,
    Reasoning,
    RunState,
    RunStatus,
    TurnResult,
)
from .tool_calls import ToolCallScheduler

if TYPE_CHECKING:
    from .react_agent import ReActAgent

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 纯决策函数
# ═══════════════════════════════════════════════════════════════

# 空响应最多重试次数（不含强制最终化那一次）。
# 超过此次数后进入"强制最终化"阶段：去掉工具，注入提示，让模型只输出文本。
MAX_EMPTY_RESPONSE_RETRIES = 2

# 强制最终化时注入 Memory 的提示词，要求模型直接给出最终回复。
FINALIZATION_RETRY_PROMPT = "请根据上面的对话，直接给出回复用户的最终内容。"

# 强制最终化后仍然返回空响应时的错误提示。
EMPTY_FINAL_RESPONSE_MESSAGE = "模型多次重试后仍未生成可见的最终文本回复。"


def decide(state: RunState, prev: TurnResult | None) -> Reasoning | Acting | Exit:
    """根据当前状态和上一轮 TurnResult 决定下一步动作。

    纯函数：不执行 I/O，不 yield 事件。
    副作用仅限修改 state.empty_retries 和 state.in_finalization。

    判断优先级（从高到低）：
        1. prev.error 非空 → Exit(ERROR)
           LLM 调用重试耗尽或遇到不可重试错误，直接退出。
        2. prev.tool_calls 非空 → Acting(tool_calls)
           模型本轮产生了工具调用，需要执行这些工具。
        3. prev.text 非空且无工具调用 → Exit(COMPLETED)
           模型本轮给出了纯文本回答，正常完成。
        4. 空响应 + in_finalization → Exit(ERROR)
           已在强制最终化阶段还是空响应，彻底失败。
        5. 空响应 + empty_retries < MAX → Reasoning() 重试
           模型返回空内容，重试计数 +1，继续推理。
        6. 空响应 + 重试耗尽 → Reasoning(最终化提示, force_no_tools)
           重试次数用尽，进入强制最终化：去掉工具，注入提示。
        7. iteration >= max_iterations → Exit(EXCEED_MAX_ITERS)
           达到最大迭代次数，防止无限循环。
        8. 默认 → Reasoning()
           首轮（prev=None）或工具执行后（prev=None），继续推理。

    Args:
        state: 当前 RunState（只读 iteration / empty_retries /
               in_finalization / runtime_context）。
        prev: 上一轮推理的 TurnResult，None 表示首轮或刚执行完工具。

    Returns:
        Reasoning: 继续调用大模型进行推理
        Acting:    执行模型产生的工具调用
        Exit:      结束（或暂停）当前回复
    """
    max_iterations = state.runtime_context.get("max_iterations")

    # 1. LLM 错误 → 直接退出
    if prev is not None and prev.error is not None:
        return Exit(
            finished_reason=ReplyFinishedReason.ERROR,
            error=f"[{prev.error.code}] {prev.error.message}",
            error_code=prev.error.code,
        )

    # 2. 有工具调用 → 执行工具
    if prev is not None and prev.tool_calls:
        return Acting(tool_calls=prev.tool_calls)

    # 3. 有非空文本且无工具调用 → 正常完成
    if prev is not None and prev.text.strip():
        return Exit(finished_reason=ReplyFinishedReason.COMPLETED)

    # 4-6. 空响应处理（prev 非空但文本为空）
    if prev is not None and not prev.text.strip():
        logger.warning(
            "[react] 空响应: text=%r reasoning=%d chars tools=%d finish_reason=%s "
            "empty_retries=%d/%d in_finalization=%s iteration=%d",
            prev.text[:80] if prev.text else "",
            len(prev.reasoning or ""),
            len(prev.tool_calls or []),
            prev.finish_reason,
            state.empty_retries,
            MAX_EMPTY_RESPONSE_RETRIES,
            state.in_finalization,
            state.iteration,
        )
        if state.in_finalization:
            # 4. 已在最终化阶段还是空 → 彻底失败
            return Exit(
                finished_reason=ReplyFinishedReason.ERROR,
                error=EMPTY_FINAL_RESPONSE_MESSAGE,
                error_code="empty_response",
            )
        if state.empty_retries < MAX_EMPTY_RESPONSE_RETRIES:
            # 5. 重试次数未达上限 → 继续 Reasoning
            state.empty_retries += 1
            return Reasoning()
        # 6. 重试耗尽 → 进入强制最终化
        state.in_finalization = True
        return Reasoning(
            hint=FINALIZATION_RETRY_PROMPT,
            force_no_tools=True,
        )

    # 7. 达到最大迭代次数
    if max_iterations is not None and state.iteration >= max_iterations:
        return Exit(finished_reason=ReplyFinishedReason.EXCEED_MAX_ITERS)

    # 8. 默认 → 继续推理
    return Reasoning()

# ReplyFinishedReason → RunStatus 映射
# EXCEED_MAX_ITERS 映射为 COMPLETED（非错误，只是达到上限）
_REASON_TO_STATUS = {
    ReplyFinishedReason.COMPLETED: RunStatus.COMPLETED,
    ReplyFinishedReason.INTERRUPTED: RunStatus.CANCELLED,
    ReplyFinishedReason.ERROR: RunStatus.ERROR,
    ReplyFinishedReason.EXCEED_MAX_ITERS: RunStatus.COMPLETED,
}

# RunStatus → Tracing RunStatus 映射
_TRACE_STATUS = {
    RunStatus.COMPLETED: TraceRunStatus.COMPLETED,
    RunStatus.CANCELLED: TraceRunStatus.CANCELLED,
    RunStatus.ERROR: TraceRunStatus.ERROR,
}


class ReActRunner:
    """ReAct Agent 的核心执行引擎。

    职责：
      - 驱动 Reason → Act → Observe 循环（通过 _decide + 执行器）
      - 管理运行锁（同一 Agent 禁止并发 run）
      - 取消入口（cancel_nowait → Task.cancel）
      - Tracing 根 span 生命周期
      - 统一终态写入（_finalize）
    """

    def __init__(self, agent: ReActAgent, *, llm: LlmAdapter | None = None):
        # 关联的 ReActAgent 实例（提供 model / memory / hooks / tracer 等依赖）
        self.agent = agent
        # 本次 run() 的可变运行状态（iteration / empty_retries / trace_span 等）
        self.state = RunState()
        # 当前 run() 对应的 asyncio.Task，用于取消和并发锁检查
        self._run_task: asyncio.Task | None = None
        # LLM 适配器（B2：协议注册表工厂按 api_type 分发，消费方零协议感知）。
        # 宿主通过统一 LLM Service 注入适配器；Runtime 不自行创建 Provider。
        if llm is None:
            raise TypeError("Runtime Agent requires an injected LLM adapter")
        self._llm: LlmAdapter = llm
        # 工具并发调度、取消传播和结果归并
        self._tool_scheduler = ToolCallScheduler(
            agent.tool_view,
            hooks=agent.hooks,
            hook_context=agent.hook_context,
        )

    @property
    def llm(self) -> LlmAdapter:
        """LLM 适配器实例（B2：LlmAdapter 契约）。"""
        return self._llm

    def set_llm(self, llm: LlmAdapter) -> None:
        """在运行前替换 LLM seam，供宿主注入统一 Service 适配器。

        过去 Host 直接写 ``runner._llm``，绕过了 Runner 的并发边界。现在
        注入优先发生在 ``ReActAgent(..., llm=...)`` 构造阶段；这个公开方法
        只保留给需要延迟装配的宿主和测试，并禁止在 in-flight run 中替换，
        避免同一轮 Reasoning 看到两个不同的 Provider。
        """
        if not callable(getattr(llm, "stream", None)):
            raise TypeError("llm must provide an async stream(messages, tools) method")
        if not callable(getattr(llm, "cancel", None)):
            raise TypeError("llm must provide cancel()")
        if self._run_task is not None and not self._run_task.done():
            raise RuntimeError("cannot replace LLM while agent is running")
        self._llm = llm

    @property
    def tool_scheduler(self) -> ToolCallScheduler:
        """工具调用调度器实例。"""
        return self._tool_scheduler

    async def run(
        self,
        message,
        runtime_context: dict | None = None,
    ) -> AsyncGenerator[AgentStreamEvent, None]:
        """启动一次 ReAct 执行，或从权限挂起中恢复。

        按输入 message 的类型分流：
          - UserConfirmResultEvent → 恢复路径：不重置状态、不写用户消息、
            复用挂起时的 reply_id，处理确认后继续；
          - 其它（str / 消息列表）→ 新回复路径：完整生命周期。

        新回复的生命周期：
          1. 并发锁检查 + 记录当前 Task
          2. 初始化 RunState（start()）
          3. 开启 Tracing 根 span
          4. 写入用户消息到 Memory
          5. 产出 ReplyStartEvent（只产一次）
          6. 驱动 _loop() 主循环
          7. 异常/取消/正常退出路径统一经过 _finalize()
          8. finally 中关闭 Tracing span + 释放运行锁

        Args:
            message: 用户消息（字符串或消息列表），或 UserConfirmResultEvent（恢复）。
            runtime_context: 调用方上下文（session_id、tracing 元数据等）。

        Yields:
            AgentStreamEvent: 回复过程中的所有流式事件。
        """
        # ── 准备阶段：按输入类型分流，产出 prologue（主循环前的前置事件流）──
        # prologue 为 None 表示无前置事件；恢复路径下它是整批工具收尾的事件流。
        prologue: AsyncGenerator[AgentStreamEvent, None] | None = None

        if isinstance(message, UserConfirmResultEvent):
            # 恢复路径：先装配运行状态（供 tracing / session_id 使用），
            # 再处理确认。runtime_context 由调用方按需传入；reply_id 由
            # _accept_confirmation 从持久化 context 推导并回填。
            self.state.runtime_context = runtime_context or {}
            self.state.runtime_context.setdefault(
                "max_iterations", self.agent.max_iterations,
            )
            # 处理确认（纯同步）。返回 False 表示仍有未决 ASKING，
            # 继续挂起、本次调用不进主循环。
            confirmation_complete = self._accept_confirmation(message)
            if not confirmation_complete:
                self.state.status = RunStatus.PAUSED
                return
            # ASKING 已清空：从 context 重建待收尾清单并整批执行，作为 prologue，
            # 随后进主循环。resume_execute 只依赖 context，不依赖实例内存。
            self._run_task = asyncio.current_task()
            self.state.status = RunStatus.RUNNING
            acting_executor = ActingExecutor(
                self.agent, self.state, self._tool_scheduler,
            )
            prologue = acting_executor.resume_execute()
        else:
            # 新回复路径：入口校验 + 初始化状态 + 写用户消息 + 产 ReplyStart。
            self._prepare_new_reply(message, runtime_context)
            yield ReplyStartEvent(
                session_id=self.state.runtime_context.get("session_id", ""),
                reply_id=self.state.reply_id,
                message_id=self.state.message_id,
                name=self.agent.model,
            )

        # ── 主循环 + 统一异常/收尾处理（新回复与恢复共用这一处）──
        session_id = self.state.runtime_context.get("session_id", "")
        reply_id = self.state.reply_id
        try:
            if prologue is not None:
                async for event in prologue:
                    yield self._annotate_event(event)

            async for event in self._loop():
                yield event

        except asyncio.CancelledError:
            # 取消路径：_finalize 设置 INTERRUPTED 状态，产出 ReplyEnd
            self._finalize(ReplyFinishedReason.INTERRUPTED)
            yield ReplyEndEvent(
                session_id=session_id, reply_id=reply_id,
                message_id=self.state.message_id,
                finished_reason=ReplyFinishedReason.INTERRUPTED,
            )

        except Exception:
            # 异常路径：_finalize 设置 ERROR 状态，产出 ReplyEnd
            self._finalize(ReplyFinishedReason.ERROR)
            yield ReplyEndEvent(
                session_id=session_id, reply_id=reply_id,
                message_id=self.state.message_id,
                finished_reason=ReplyFinishedReason.ERROR,
                error={"message": str(self.state.error or "Unknown error")},
            )
            raise

        finally:
            # Tracing 收尾：依据 RunState.status 映射 trace 状态并关闭根 span。
            # PAUSED 例外——权限挂起不是终态，span 要保持开着供恢复阶段复用，不在此关闭。
            if (
                self.state.status != RunStatus.PAUSED
                and self.state.trace_span
                and not self.state.trace_span.ended
            ):
                self.state.trace_span.end(
                    status=_TRACE_STATUS.get(self.state.status, TraceRunStatus.ERROR),
                    outputs={
                        "success": self.state.status == RunStatus.COMPLETED,
                        "done_reason": self.state.done_reason,
                        "iterations": self.state.iteration,
                    },
                    error=self.state.error if self.state.status == RunStatus.ERROR else None,
                )
            # 释放运行锁。挂起时也释放，使恢复调用能重新获取（恢复走独立入口）。
            self._run_task = None

    def _prepare_new_reply(self, message, runtime_context: dict | None) -> None:
        """新回复的准备阶段（纯同步）：入口校验、初始化状态、开 span、写用户消息。

        完成后 self.state 已 start()、trace_span 已开启、用户消息已写入 context、
        reply_id 已生成。ReplyStartEvent 由调用方 run() 负责产出。
        """
        # ── 入口校验：处于挂起态时，非确认输入一律拒绝 ──
        # 只要 context 里还有 ASKING 的工具调用，就必须先用 UserConfirmResultEvent
        # 解决它，不能用新消息覆盖挂起。
        if MessageContext.tool_calls_in_state(
            self.agent.state.context, ToolCallState.ASKING
        ):
            raise RuntimeError(
                "Agent is awaiting permission confirmation; "
                "send a UserConfirmResultEvent, not a new message."
            )

        # ── 并发锁：同一 Agent 禁止并发 run() ──
        if self._run_task is not None and not self._run_task.done():
            raise RuntimeError("Agent is already running")

        # 记录当前 Task，供 cancel_nowait() 使用
        self._run_task = asyncio.current_task()
        self.state.runtime_context = runtime_context or {}
        # max_iterations 放入 runtime_context 供 _decide() 读取
        self.state.runtime_context.setdefault(
            "max_iterations", self.agent.max_iterations,
        )
        self.state.start()

        # ── 准备 Tracing 元数据 ──
        # 调用方可通过 runtime_context 传入自定义 trace 元数据，
        # 这里兜底处理：非 dict 自动包一层，然后强制带上 model / api_type
        trace_metadata = self.state.runtime_context.get("trace_metadata") or {}
        if not isinstance(trace_metadata, dict):
            trace_metadata = {"value": trace_metadata}
        trace_metadata = {
            "model": self.agent.model,
            "api_type": self.agent.api_type,
            **trace_metadata,  # 调用方自定义字段可覆盖上面的默认值
        }
        # tags 允许传单个字符串，统一规整成 list
        trace_tags = self.state.runtime_context.get("trace_tags") or []
        if isinstance(trace_tags, str):
            trace_tags = [trace_tags]

        # 开启根 span（AGENT 节点）；tracer 未配置 exporter 时为空操作
        self.state.trace_span = self.agent.tracer.start_run(
            str(self.state.runtime_context.get("trace_name") or "react_agent"),
            RunType.AGENT,
            inputs={"message": message},
            metadata=trace_metadata,
            tags=list(trace_tags),
        )

        # ── 将用户消息写入 AgentState.context ──
        if isinstance(message, str):
            MessageContext.add_user(self.agent.state.context, message)
        else:
            # 列表形式：原样写入 context（含 system 消息等）
            for msg in message:
                MessageContext.add_raw(self.agent.state.context, msg)

        # ── 生成稳定 run reply_id 与首个 Assistant message_id ──
        self.state.reply_id = uuid.uuid4().hex[:16]
        self.state.message_id = uuid.uuid4().hex[:16]

    def _accept_confirmation(self, event: UserConfirmResultEvent) -> bool:
        """处理一条用户确认结果（纯同步，不产事件）。

        返回值：
          - False → 仍有未决 ASKING，继续挂起，本次调用不进主循环；
          - True  → ASKING 已全部清空，可从 context 重建清单整批执行。

        校验失败（非挂起态 / reply_id 或 tool_call_id 不匹配）直接抛 RuntimeError。
        待恢复清单完全由 context 推导，本方法只负责更新目标 tool_call 的状态。
        """
        context = self.agent.state.context
        target = next(
            (
                block
                for message in context
                for block in message.content
                if isinstance(block, ToolCallBlock) and block.id == event.tool_call_id
            ),
            None,
        )
        if target is None:
            raise RuntimeError(
                f"UserConfirmResultEvent.tool_call_id {event.tool_call_id!r} "
                f"is not awaiting confirmation."
            )
        # 采信事件携带的 reply_id 并回填到运行状态。ToolCall 所在的 Msg.id
        # 同时恢复当前 message_id，避免权限恢复把结果写到另一条 AssistantMsg。
        if event.reply_id:
            self.state.reply_id = event.reply_id
        owner = next(
            (
                message
                for message in context
                if any(
                    isinstance(block, ToolCallBlock) and block.id == event.tool_call_id
                    for block in message.content
                )
            ),
            None,
        )
        if owner is not None:
            self.state.message_id = owner.id

        # 两种入口都必须支持：
        # 1) Runtime 直接使用：宿主直接调用 agent.run(UserConfirmResultEvent)，
        #    context 中仍是 ASKING，由 Runtime 在下面应用本次确认。
        # 2) FTRE 等持久化宿主：为了防止用户确认后进程立即崩溃，宿主会先把
        #    确认结果 checkpoint 为 ALLOWED / FINISHED，再新建 Agent 恢复。
        #    此时同一决定已经存在于 context，应按幂等输入继续执行，而不是报错。
        expected = ToolCallState.ALLOWED if event.approved else ToolCallState.FINISHED
        logger.info(
            "[permission] accept confirmation tool_call_id=%s current=%s expected=%s",
            event.tool_call_id,
            target.state,
            expected,
        )
        if target.state == ToolCallState.ASKING:
            # Runtime 直接调用路径：确认结果尚未由宿主持久化，Runtime 自己更新状态。
            logger.info(
                "[permission] apply pending confirmation tool_call_id=%s approved=%s",
                event.tool_call_id,
                event.approved,
            )
            MessageContext.set_tool_call_state(context, event.tool_call_id, expected)
        elif target.state != expected:
            raise RuntimeError(
                f"UserConfirmResultEvent.tool_call_id {event.tool_call_id!r} "
                "conflicts with the persisted confirmation state."
            )
        else:
            # FTRE 路径：宿主已先持久化相同决定，本次输入只负责驱动恢复执行。
            logger.info(
                "[permission] confirmation already checkpointed tool_call_id=%s",
                event.tool_call_id,
            )

        # 仍有未决 ASKING → 继续挂起（保持 PAUSED）；否则可整批收尾
        return not MessageContext.tool_calls_in_state(context, ToolCallState.ASKING)

    def cancel_nowait(self) -> None:
        """外部调用：取消当前执行。

        对 self._run_task 执行 Task.cancel()，CancelledError 会沿
        await 调用栈传播到 _loop() 或 _execute_reasoning / _execute_acting
        中的当前 await 点，最终被 run() 的 except 捕获并转换为
        INTERRUPTED 结果。
        """
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    def _annotate_event(self, event: AgentStreamEvent) -> AgentStreamEvent:
        """给流事件补上当前 AssistantMsg 坐标。

        ``reply_id`` 贯穿整个 run；事件实际落到哪个 AssistantMsg 由
        ``message_id`` 决定。统一在 Runner 出口补字段，避免每个执行器重复
        传递同一个短生命周期坐标。
        """
        if getattr(event, "reply_id", None):
            return event.model_copy(
                update={"message_id": self.state.message_id or event.reply_id}
            )
        return event

    async def _dispatch_before_reasoning(self) -> BeforeReasoningResult:
        """在每次真实 LLM Reasoning 前消费宿主贡献的上下文。

        这是 Runtime 唯一需要知道的 active-step 边界：队列、claim 和消息来源
        都留在宿主 Hook。取消一旦已经发出，不再让 Hook 注入新的消息。
        """
        cancellation = self.state.runtime_context.get("cancellation")
        if not isinstance(cancellation, asyncio.Event):
            cancellation = asyncio.Event()
        if self.state.is_cancelled or cancellation.is_set():
            raise asyncio.CancelledError

        payload = BeforeReasoningPayload(
            agent=self.state.runtime_context.get("agent_subject", self.agent),
            session_id=str(self.state.runtime_context.get("session_id", "")),
            turn_id=self.state.turn_id,
            iteration=self.state.iteration,
            cancellation=cancellation,
        )
        AGENT_BEFORE_REASONING_SPEC.validate_payload(payload)
        if self.agent.hooks is None:
            result = await AGENT_BEFORE_REASONING_SPEC.default(payload)
        else:
            result = await self.agent.hooks.dispatch(
                AGENT_BEFORE_REASONING_SPEC,
                payload,
                context=self.agent.hook_context,
            )
        AGENT_BEFORE_REASONING_SPEC.validate_result(result)
        if self.state.is_cancelled or cancellation.is_set():
            raise asyncio.CancelledError
        return result

    async def _loop(self) -> AsyncGenerator[AgentStreamEvent, None]:
        """ReAct 主循环：Reason → Act → Observe。

        循环逻辑：
          1. 调用 _decide(state, prev) 获取下一步动作
          2. 根据动作类型分发到对应执行器
          3. Reasoning → 递增 iteration，调 LLM，更新 prev
          4. Acting    → 执行工具，清除 prev（下一轮重新推理）
          5. Exit      → 通过 stop-decision 决策后产出结束事件，return

        iteration 计数规则：
          只在 Reasoning 时递增。一次"迭代"= 一次 LLM 调用，
          可能后跟一次 Acting（工具执行），但不额外计数。
          这样 max_iterations=N 表示最多调用 N 次 LLM。

        Exit + should_continue 的特殊路径：
          stop-decision 返回 ContinueTurn 时，ExitExecutor 产出 HintBlockEvent
          但不产 ReplyEndEvent，返回 ExitOutcome(should_continue=True)。
          主循环注入续写提示到 Memory，清除 prev，继续循环。
        """
        prev: TurnResult | None = None
        # 创建三个执行器实例（循环内复用）
        reasoning_executor = ReasoningExecutor(
            self.agent, self.state, self._llm, self.agent.hooks, self.agent.hook_context,
        )
        acting_executor = ActingExecutor(
            self.agent, self.state, self._tool_scheduler,
        )
        exit_executor = ExitExecutor(
            self.agent, self.state, self.agent.hooks, self.agent.hook_context,
        )

        try:
            while True:
                # 纯决策函数：只读状态，返回动作类型
                action = decide(self.state, prev)

                if isinstance(action, Reasoning):
                    # ── Reasoning：调 LLM ──
                    self.state.iteration += 1
                    before_reasoning = await self._dispatch_before_reasoning()
                    # Hook 返回的是 Provider 无关的只读 mapping；转换为普通 dict
                    # 后交给 MessageContext，确保 Runtime 不把宿主的 mapping 作为可变
                    # Memory 对象长期持有。
                    for message in before_reasoning.messages:
                        MessageContext.add_raw(
                            self.agent.state.context,
                            dict(message),
                        )
                    # 正式 UserMessage 是真实对话边界。当前已存在 AssistantMsg 时，
                    # 下一次 Reasoning 使用新的 message_id；system/assistant 贡献不
                    # 凭空制造用户可见的 Assistant 气泡。
                    had_current_assistant = any(
                        message.role == "assistant"
                        and message.id == self.state.message_id
                        for message in self.agent.state.context
                    )
                    has_user_message = any(
                        str(message.get("role", "")) == "user"
                        for message in before_reasoning.messages
                    )
                    if had_current_assistant and has_user_message:
                        self.state.message_id = uuid.uuid4().hex[:16]
                    # 执行 LLM 调用 + 流式消费 + 重试
                    async for event in reasoning_executor.stream(action):
                        yield self._annotate_event(event)
                    # 获取本轮推理的结构化产物，供下一轮 _decide() 消费
                    prev = reasoning_executor.result

                elif isinstance(action, Acting):
                    # ── Acting：执行工具（或因权限挂起）──
                    # 并发执行所有工具调用，成组写入 Memory。
                    # 若本轮存在 ASK/DENY，stream 会 yield RequireUserConfirmEvent
                    # 表示整批挂起、未执行任何工具。
                    paused = False
                    async for event in acting_executor.stream(action):
                        yield self._annotate_event(event)
                        if isinstance(event, RequireUserConfirmEvent):
                            paused = True
                    if paused:
                        # 权限挂起：置 PAUSED 状态后跳出循环。
                        # 【不】finalize、【不】产 ReplyEnd——挂起不是回复结束。
                        # 待恢复清单已随 ASKING 状态写入 context，恢复时从 context 重建，
                        # 不在实例内存里保存动作（进程重启后也能恢复）。
                        self.state.status = RunStatus.PAUSED
                        return
                    # 清除 prev：工具执行后需要重新调 LLM 读取工具结果
                    prev = None

                elif isinstance(action, Exit):
                    # ── Exit：结束（或暂停）当前回复 ──
                    async for event in exit_executor.stream(action):
                        yield self._annotate_event(event)
                    # ContinueTurn 返回时不退出
                    if exit_executor.outcome.should_continue:
                        # 注入续写提示已在 ExitExecutor 中完成
                        prev = None
                        continue
                    return

        except CancelledError:
            # 内部取消异常（来自 _execute_acting 的 cancelled=True 路径）
            self._finalize(ReplyFinishedReason.INTERRUPTED)
            yield ReplyEndEvent(
                session_id=self.state.runtime_context.get("session_id", ""),
                reply_id=self.state.reply_id,
                message_id=self.state.message_id,
                finished_reason=ReplyFinishedReason.INTERRUPTED,
            )

    def _finalize(self, reason: ReplyFinishedReason) -> None:
        """统一终态写入。

        所有退出路径（正常、取消、异常、超限）都经过此函数，
        确保 RunState 的 status 和 done_reason 一致。

        Args:
            reason: 结束原因（COMPLETED / INTERRUPTED / ERROR / EXCEED_MAX_ITERS）
        """
        self.state.done_reason = reason
        self.state.status = _REASON_TO_STATUS.get(reason, RunStatus.ERROR)
