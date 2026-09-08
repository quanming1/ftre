"""LLM 调用执行器（Reasoning 动作）——会话事件版。

职责链路：

    准备 messages + tools（hint 先入 memory）
    → LLM stream（BlockAssembler 组装；参数流式只在进程内）
    → 重试循环（LLM_ERROR Hook 决策）
    → 流式 yield 会话事件（PRD-F41 附录 A）：
        text/reasoning delta → assistant/chunk
        tool-call 定稿       → tool/call-start（whole-value arguments）
        Turn 语义边界         → assistant/message（由 TurnExecutor 统一收口）
        LLM 重试             → turn/retry
        hint                 → hint/message
    → 组装 TurnResult 写入 self.result

本模块只负责"一次 Reasoning 动作"的执行细节，不关心多轮编排。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ftre_agent.hooks import (
    AGENT_CONTEXT_BUILD_SPEC,
    LLM_ERROR_SPEC,
    LLM_STREAM_SPEC,
    ContextBuildPayload,
    ContextBuildResult,
    HookDispatcher,
    LLMErrorDecision,
    LLMErrorPayload,
    LLMStreamPayload,
)
from ftre_agent.message import (
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
)
from ftre_agent.session.events import (
    AssistantChunk,
    AssistantChunkData,
    HintData,
    HintMessage,
    ToolCallStart,
    ToolCallStartData,
    TurnRetry,
    TurnRetryData,
)
from ftre_agent.tracing import RunStatus as TraceRunStatus
from ftre_agent.tracing import RunType
from ftre_llm import (
    BlockAssembler,
    BlockEnd,
    FinishChunk,
    LlmAdapter,
    LLMError,
    ReasoningDeltaChunk,
    TextDeltaChunk,
    ToolCall,
    ToolCallDeltaChunk,
    UsageChunk,
)

from ..message_context import MessageContext
from ..run_state import Reasoning, TurnResult

if TYPE_CHECKING:
    from ..run_state import RunState

logger = logging.getLogger(__name__)


class ReasoningExecutor:
    """执行 Reasoning 动作的核心执行器。"""

    def __init__(
        self,
        agent,
        state: RunState,
        llm: LlmAdapter,
        hooks: HookDispatcher | None = None,
        hook_context: object | None = None,
    ):
        self.agent = agent
        self.state = state
        self.llm = llm
        self.hooks = hooks
        self.hook_context = hook_context
        self.result: TurnResult | None = None

    async def _build_context_view(self) -> list[Msg]:
        """构建一次本轮 LLM 使用的 Msg 视图，Retry 复用该结果。

        AgentState.context 始终保留完整历史；插件只能修改这里的深拷贝。
        这样压缩、脱敏等策略不会污染 Session Snapshot，也不会在每次
        Provider retry 时重复执行。
        """
        base_context = tuple(
            message.model_copy(deep=True) for message in self.agent.state.context
        )
        cancellation = self.state.runtime_context.get("cancellation")
        if not isinstance(cancellation, asyncio.Event):
            cancellation = asyncio.Event()
        payload = ContextBuildPayload(
            session_id=str(self.state.runtime_context.get("session_id", "")),
            turn_id=self.state.turn_id,
            request_id=str(self.state.runtime_context.get("request_id") or ""),
            iteration=self.state.iteration,
            model=getattr(self.llm, "model", "") or self.agent.model,
            messages=base_context,
            context_limit=self.state.runtime_context.get("context_limit"),
            cancellation=cancellation,
        )
        if self.hooks is None:
            result = await AGENT_CONTEXT_BUILD_SPEC.default(payload)
        else:
            result = await self.hooks.dispatch(
                AGENT_CONTEXT_BUILD_SPEC,
                payload,
                context=self.hook_context,
            )
        # OBSERVE 型监听器失败时 HookRuntime 可能返回 None；安全地回到
        # 完整上下文，而不是让可选压缩插件阻断 Agent。
        if result is None:
            result = ContextBuildResult(base_context)
        AGENT_CONTEXT_BUILD_SPEC.validate_result(result)
        return [message.model_copy(deep=True) for message in result.messages]

    async def _stream(self, messages, tools, *, attempt: int, max_attempts: int):
        cancellation = self.state.runtime_context.get("cancellation")
        if not isinstance(cancellation, asyncio.Event):
            cancellation = asyncio.Event()
        if self.hooks is None:
            async for chunk in self.llm.stream(messages, tools):
                yield chunk
            return
        payload = LLMStreamPayload(
            agent_id=str(self.state.runtime_context.get("agent_id", "")),
            session_id=str(self.state.runtime_context.get("session_id", "")),
            turn_id=self.state.turn_id,
            provider=str(getattr(self.llm, "provider", "")),
            model=getattr(self.llm, "model", ""),
            purpose="conversation",
            messages=tuple(messages),
            tools=tuple(tools or ()),
            cancellation=cancellation,
            invoke=lambda: self.llm.stream(messages, tools),
            attempt=attempt,
            max_attempts=max_attempts,
        )
        stream = await self.hooks.dispatch(
            LLM_STREAM_SPEC,
            payload,
            context=self.hook_context,
        )
        async for chunk in stream:
            yield chunk

    async def stream(self, action: Reasoning) -> AsyncGenerator[Any, None]:
        """执行一次 LLM 调用，流式 yield 会话事件，结束后设置 ``self.result``。"""
        message_id = self.state.message_id or self.state.reply_id
        model_name = self.agent.model

        # ── 阶段 1：hint 写入 memory + hint/message 事件 ──────────────────
        if action.hint:
            hint_block = HintBlock(
                id=uuid.uuid4().hex[:16],
                source="system",
                hint=action.hint,
            )
            MessageContext.append_reply_blocks(
                self.agent.state.context,
                message_id,
                [hint_block],
            )
            yield HintMessage(
                data=HintData(hint=action.hint, source="system"),
                message_id=message_id,
            )

        # ── 阶段 2：准备完整 Msg ContextView + tools ─────────────────────
        # context-build 只在这一轮开始时执行一次；后续 Provider retry
        # 复用同一份 provider messages。
        context_view = await self._build_context_view()
        messages = MessageContext.get_messages(context_view, self.agent.system_prompt)
        tools = None if action.force_no_tools else self.agent.tool_view.to_openai_tools() or None

        max_attempts = 1 + self.agent.max_retries
        turn_start_ts = time.perf_counter()
        first_token_logged = False

        # ── 阶段 3：重试循环 ─────────────────────────────────────────────
        for attempt in range(max_attempts):
            # 每次 retry 都是一次独立的 provider 响应。失败尝试已经通过
            # assistant/chunk 发给客户端，但不能把它的正文/工具调用拼进
            # 下一次成功的 whole-value Assistant 快照。
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            finish_reason = "unknown"
            usage: dict | None = None
            response_metadata: dict = {}
            llm_span = None
            if self.state.trace_span:
                llm_span = self.state.trace_span.child(
                    "llm", RunType.LLM,
                    inputs={"messages": messages, "tools": tools},
                    metadata={
                        "model": model_name,
                        "api_type": self.agent.api_type,
                        "iteration": self.state.iteration,
                        "attempt": attempt + 1,
                    },
                )

            try:
                text_block_id: str | None = None
                thinking_block_id: str | None = None
                tool_call_started: set[str] = set()
                assembler = BlockAssembler()

                # ── 阶段 4：流式消费 StreamChunk ───────────────────────
                async for chunk in self._stream(
                    messages,
                    tools,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                ):
                    if not first_token_logged:
                        first_token_logged = True
                        elapsed_ms = (time.perf_counter() - turn_start_ts) * 1000
                        logger.info(
                            "[react] 第 %d 轮 TTFT %.0fms",
                            self.state.iteration, elapsed_ms,
                        )
                        if llm_span and not llm_span.ended:
                            llm_span.add_event("ttft", {"ms": round(elapsed_ms)})

                    assembler.feed(chunk)

                    # ① 正文文本增量：首个 delta 携带 block_id（客户端按需开块）
                    if isinstance(chunk, TextDeltaChunk):
                        text_parts.append(chunk.text)
                        if text_block_id is None:
                            text_block_id = uuid.uuid4().hex[:16]
                        yield AssistantChunk(
                            data=AssistantChunkData(
                                kind="text", delta=chunk.text, block_id=text_block_id
                            ),
                            message_id=message_id,
                        )

                    # ② 推理文本增量
                    elif isinstance(chunk, ReasoningDeltaChunk):
                        reasoning_parts.append(chunk.text)
                        if thinking_block_id is None:
                            thinking_block_id = uuid.uuid4().hex[:16]
                        yield AssistantChunk(
                            data=AssistantChunkData(
                                kind="thinking",
                                delta=chunk.text,
                                block_id=thinking_block_id,
                            ),
                            message_id=message_id,
                        )

                    # ③ 工具入参增量：不上 wire（定稿时 whole-value）
                    elif isinstance(chunk, ToolCallDeltaChunk):
                        del chunk

                    # ④ 块闭合：tool-call 定稿 → tool/call-start（完整参数）
                    elif isinstance(chunk, BlockEnd):
                        block = chunk.block or {}
                        block_type = block.get("type")
                        if block_type == "tool-call":
                            call_id = block.get("id", "")
                            name = block.get("name", "")
                            arguments = block.get("arguments", "")
                            try:
                                parsed = json.loads(arguments) if arguments else {}
                            except json.JSONDecodeError:
                                logger.warning(
                                    "[react] 工具 %s 的 JSON 参数解析失败: %r",
                                    name, arguments[:200],
                                )
                                parsed = None
                            if call_id not in tool_call_started:
                                tool_call_started.add(call_id)
                                yield ToolCallStart(
                                    data=ToolCallStartData(
                                        tool_call_id=call_id,
                                        name=name,
                                        arguments=parsed if isinstance(parsed, dict) else {},
                                    ),
                                    message_id=message_id,
                                )
                            tool_calls.append(ToolCall(id=call_id, name=name, input=parsed))
                        elif block_type == "text":
                            text_block_id = None
                        elif block_type == "thinking":
                            thinking_block_id = None

                    # ⑤ usage：记录（协议保证在 finish 之前）
                    elif isinstance(chunk, UsageChunk):
                        usage = chunk.usage

                    # ⑥ finish：error/aborted 还原为异常走重试路径
                    elif isinstance(chunk, FinishChunk):
                        reason = chunk.reason
                        if reason.kind in ("error", "aborted"):
                            failure = reason.failure
                            raise LLMError(
                                message=failure.message if failure else reason.kind,
                                code=failure.code if failure else reason.kind.upper(),
                            )
                        finish_reason = reason.kind
                        response_metadata = reason.response_metadata
                        required_usage_fields = {
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                        }
                        valid_usage = (
                            isinstance(usage, dict)
                            and required_usage_fields.issubset(usage)
                        )
                        if valid_usage:
                            normalized_usage = {
                                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                                "total_tokens": int(usage.get("total_tokens", 0) or 0),
                            }
                            self.state.last_call_usage = normalized_usage
                            self.state.token_usage["prompt_tokens"] += normalized_usage["prompt_tokens"]
                            self.state.token_usage["completion_tokens"] += normalized_usage["completion_tokens"]
                            self.state.token_usage["total_tokens"] += normalized_usage["total_tokens"]
                        else:
                            logger.warning(
                                "LLM 调用未返回完整 token usage，忽略本次用量: "
                                "model=%s required=%s",
                                self.agent.model,
                                sorted(required_usage_fields),
                            )

                # ── 阶段 5：成功完成 ───────────────────────────────────
                assembler.validate()

                full_text = "".join(text_parts)
                full_reasoning = "".join(reasoning_parts)

                # max-tokens 截断：不完整 JSON 参数的 tool-call 整体丢弃。
                if finish_reason == "max-tokens" and tool_calls:
                    logger.warning(
                        "[react] max-tokens 截断，丢弃 %d 个不完整工具调用: %s",
                        len(tool_calls),
                        [tc.name for tc in tool_calls],
                    )
                    tool_calls = []
                if llm_span and not llm_span.ended:
                    llm_span.end(outputs={
                        "text": full_text,
                        "reasoning": full_reasoning,
                        "finish_reason": finish_reason,
                        "has_tool_calls": bool(tool_calls),
                        "usage": usage,
                        "response_metadata": response_metadata,
                    })

                response_blocks = []
                if full_reasoning:
                    response_blocks.append(ThinkingBlock(thinking=full_reasoning))
                if full_text:
                    response_blocks.append(TextBlock(text=full_text))
                response_blocks.extend(
                    ToolCallBlock(
                        id=tool_call.id,
                        name=tool_call.name,
                        arguments=tool_call.input or {},
                    )
                    for tool_call in tool_calls
                )
                MessageContext.append_reply_blocks(
                    self.agent.state.context,
                    message_id,
                    response_blocks,
                )

                self.result = TurnResult(
                    text=full_text,
                    reasoning=full_reasoning,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    usage=usage,
                )
                return

            # ── 阶段 6a：CancelledError 路径 ──────────────────────────────
            except asyncio.CancelledError:
                if llm_span and not llm_span.ended:
                    llm_span.end(status=TraceRunStatus.CANCELLED)
                _full_text = "".join(text_parts)
                _full_reasoning = "".join(reasoning_parts)
                partial_blocks = []
                if _full_reasoning:
                    partial_blocks.append(ThinkingBlock(thinking=_full_reasoning))
                if _full_text:
                    partial_blocks.append(TextBlock(text=_full_text))
                if partial_blocks:
                    MessageContext.append_reply_blocks(
                        self.agent.state.context,
                        message_id,
                        partial_blocks,
                    )
                raise

            # ── 阶段 6b：其他异常路径 ─────────────────────────────────────
            except Exception as exc:  # noqa: BLE001 - normalize provider failures
                if llm_span and not llm_span.ended:
                    llm_span.end(error=exc)

                _full_text = "".join(text_parts)
                _full_reasoning = "".join(reasoning_parts)
                partial_blocks = []
                if _full_reasoning:
                    partial_blocks.append(ThinkingBlock(thinking=_full_reasoning))
                if _full_text:
                    partial_blocks.append(TextBlock(text=_full_text))
                err = exc if isinstance(exc, LLMError) else LLMError.classify(exc)
                is_last = attempt >= max_attempts - 1

                decision = await self._dispatch_llm_error(
                    err,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                )

                logger.warning(
                    "LLM 调用失败 [%s] %s (第 %d/%d 次尝试)",
                    err.code, err.message[:200], attempt + 1, max_attempts,
                )

                should_retry = (
                    err.code not in LLMError.UNRETRYABLE_CODES
                    and not is_last
                )
                if decision is not None:
                    should_retry = decision.action == "retry"
                if is_last or self.state.is_cancelled:
                    should_retry = False

                if not should_retry:
                    if partial_blocks:
                        MessageContext.append_reply_blocks(
                            self.agent.state.context,
                            message_id,
                            partial_blocks,
                        )
                    self.result = TurnResult(
                        text="",
                        reasoning="",
                        tool_calls=[],
                        finish_reason="error",
                        error=err,
                    )
                    return

                # 可重试 → turn/retry 事件（UI 重试横幅唯一数据源）
                yield TurnRetry(
                    data=TurnRetryData(
                        turn_id=str(self.state.runtime_context.get("turn_id") or ""),
                        code=err.code,
                        message=err.message,
                        attempt=attempt + 1,
                        max_attempts=max_attempts - 1,
                    )
                )
                delay = self.agent.retry_delay
                if decision is not None and decision.delay is not None:
                    try:
                        delay = max(0.0, float(decision.delay))
                    except (TypeError, ValueError):
                        delay = self.agent.retry_delay
                await asyncio.sleep(delay)
                # Provider retry 复用本轮已构建的 ContextView，避免重复执行
                # 压缩/脱敏等 Hook；只有下一轮 Reasoning 才重新构建。
                text_parts = []
                reasoning_parts = []
                tool_calls = []
                finish_reason = "unknown"
                usage = None
                response_metadata = {}

    async def _dispatch_llm_error(
        self,
        error: LLMError,
        *,
        attempt: int,
        max_attempts: int,
    ) -> LLMErrorDecision | None:
        """发布一次 LLM 失败决策 Hook，并在可选 Plugin 故障时回到默认策略。"""

        cancellation = self.state.runtime_context.get("cancellation")
        if not isinstance(cancellation, asyncio.Event):
            cancellation = asyncio.Event()
        if self.state.is_cancelled or cancellation.is_set():
            return None

        payload = LLMErrorPayload(
            session_id=str(self.state.runtime_context.get("session_id", "")),
            turn_id=self.state.turn_id,
            iteration=self.state.iteration,
            model=getattr(self.llm, "model", ""),
            error_code=error.code,
            error_message=error.message,
            attempt=attempt,
            max_attempts=max_attempts,
            cancellation=cancellation,
            agent_id=str(self.state.runtime_context.get("agent_id", "")),
        )
        try:
            if self.hooks is None:
                result = await LLM_ERROR_SPEC.default(payload)
            else:
                result = await self.hooks.dispatch(
                    LLM_ERROR_SPEC,
                    payload,
                    context=self.hook_context,
                )
            LLM_ERROR_SPEC.validate_result(result)
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            # Retry Policy 是可选行为；监听器异常不能把原始 LLM 错误升级成
            # 另一种 Agent 异常，Runtime 回到原有默认分类。
            logger.exception(
                "[llm/error] listener failed session=%s attempt=%s/%s",
                payload.session_id,
                payload.attempt,
                payload.max_attempts,
            )
            return None
