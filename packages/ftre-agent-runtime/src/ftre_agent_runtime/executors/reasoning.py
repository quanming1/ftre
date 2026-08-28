"""LLM 调用执行器（Reasoning 动作）。

整体职责链路如下：

    准备 messages + tools
        └─ 将 hint（若有）写入 memory，使其对后续 LLM 可见；从 memory 拉取最新
           messages；依据 force_no_tools / tool_view 决定是否附带 tools。
    → LLM stream
        └─ 通过 self.llm.stream(messages, tools) 异步迭代获取 StreamChunk 流
           （B2：DSH StreamChunk 协议，BlockAssembler 组装完整块）。
    → 重试循环
        └─ 以 max_attempts = 1 + max_retries 为上限反复尝试，直到成功或耗尽。
    → 流式 yield 事件
        └─ 将七种 chunk（block-start / text-delta / reasoning-delta /
           tool-call-delta / block-end / usage / finish）转译为面向上层
           （UI / 调用方）的 AgentStreamEvent；error finish 触发重试路径。
    → 组装 TurnResult
        └─ 成功时把文本、推理、工具调用、finish_reason、usage 封装为 TurnResult，
           写入 self.result；失败且不可重试或耗尽时封装带 error 的 TurnResult。

本模块只负责"一次 Reasoning 动作"的执行细节，不关心多轮编排（那是 runner 的职责）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from ftre_agent.event import (
    AgentStreamEvent,
    HintBlockEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    RetryEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from ftre_agent.hooks import (
    LLM_ERROR_SPEC,
    LLM_STREAM_SPEC,
    HookDispatcher,
    LLMErrorDecision,
    LLMErrorPayload,
    LLMStreamPayload,
)
from ftre_agent.message import HintBlock, TextBlock, ThinkingBlock, ToolCallBlock
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
    """执行 Reasoning 动作的核心执行器。

    职责：在给定 agent 状态（RunState）下，调用 LLM 进行流式推理，把模型增量输出
    转译为 AgentStreamEvent 向上游 yield，并在结束后把本轮结果封装为 TurnResult
    写入 ``self.result``。

    生命周期：由 runner 在每次需要做一次"推理动作"时实例化，调用 ``stream()``
    消费完事件后即可读取 ``self.result`` 获取结构化结果。实例本身是一次性的，
    不应跨多轮复用。
    """

    def __init__(
        self,
        agent,
        state: RunState,
        llm: LlmAdapter,
        hooks: HookDispatcher | None = None,
        hook_context: object | None = None,
    ):
        """初始化执行器。

        参数：
            agent: 当前 Agent 实例。从中读取 model（模型名）、api_type、tool_view、
                max_retries、retry_delay 以及 memory（对话记忆）等运行时配置。
            state: 当前一次 run 的共享状态（RunState）。包含 reply_id、iteration、
                trace_span（可选的追踪 span）、token_usage（跨轮累计的 token 用量）等。
            llm: LLM 适配器实例（B2：LlmAdapter 契约），提供 ``stream(messages, tools)``
                异步迭代接口，产出 StreamChunk（七种 chunk，BlockAssembler 组装）。
            hooks: 宿主注入的 Hook Dispatcher。
            hook_context: 宿主 scope carrier，Runtime 不解释其具体类型。

        属性：
            self.result: 本轮推理的最终结果（TurnResult | None）。在 ``stream()``
                结束前为 None；无论成功还是失败，``stream()`` 正常返回后都会被
                设置为对应的 TurnResult（成功含文本/工具调用，失败含 error）。
        """
        self.agent = agent
        self.state = state
        self.llm = llm
        self.hooks = hooks
        self.hook_context = hook_context
        self.result: TurnResult | None = None

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

    async def stream(self, action: Reasoning) -> AsyncGenerator[AgentStreamEvent, None]:
        """执行一次 LLM 调用，流式 yield 事件，结束后设置 ``self.result``。

        执行流程分为 6 个阶段：

        1. hint 写入 memory + yield HintBlockEvent
            若 action.hint 非空，先以原始 user 消息形式写入 memory（必须在 LLM
            调用之前完成，这样模型才能"看到"该提示），并向下游 yield 一个标记为
            hide/internal 的 HintBlockEvent 供 UI 隐藏展示。

        2. 准备 messages + tools
            从 memory 拉取最新 messages；tools 由 force_no_tools 决定——当
            force_no_tools=True 或 tool_view 为空时 tools 为 None（纯文本对话），
            否则转换为 OpenAI 工具描述列表。

        3. 重试循环
            max_attempts = 1 + max_retries，逐次尝试调用 LLM，直到成功返回或耗尽次数。

        4. 流式消费 LLM 输出
            迭代 self.llm.stream(...) 产出的 StreamChunk 并分别处理：
              - text-delta        → 累积正文文本，按需开启 TextBlock 并 yield 增量
              - reasoning-delta   → 累积推理文本，按需开启 ThinkingBlock 并 yield 增量
              - tool-call-delta   → 工具入参的流式增量，首次出现时发 ToolCallStart
              - block-end(tool)   → 工具调用完成，发 ToolCallEnd 并收录到 tool_calls
              - usage             → 记录 token 用量（finish 之前）
              - finish            → 本步结束：记录 finish_reason、关闭仍开启的 block、
                                    yield ModelCallEndEvent；error/aborted 还原为
                                    LLMError 走统一重试路径

        5. 成功完成
            关闭 LLM span（输出 text/reasoning/finish_reason/has_tool_calls/usage/
            response_metadata），把本次 Provider 响应的 thinking/text/tool_calls
            作为内容块追加到当前 message_id 对应的 assistant Msg。最后组装成功
            TurnResult 写入 self.result 并 return。

        6. 异常处理
            - CancelledError：关闭 span（CANCELLED 状态）→ 收尾 open blocks → 把
              已生成的半截文本写入 memory → re-raise（让上游感知取消）。
            - 其他 Exception：关闭 span（带 error）→ 收尾 open blocks → 写半截文本
              → LLMError.classify 归类 → 若不可重试或次数耗尽则组装带 error 的
              TurnResult 返回；否则 yield RetryEvent → sleep(retry_delay) → 重置
              收集器后进入下一轮尝试。
        """
        reply_id = self.state.reply_id
        message_id = self.state.message_id or self.state.reply_id
        model_name = self.agent.model

        # ── 阶段 1：hint 写入 memory + yield HintBlockEvent ──────────────────────
        # hint 必须在 LLM 调用之前写入 memory，这样它才会出现在传给模型的 messages 中，
        # 对模型可见；否则模型无法据此调整行为。同时向下游发一个 HintBlockEvent，
        # 标记 hide/internal 以便 UI 层隐藏渲染。
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
            yield HintBlockEvent(
                reply_id=reply_id,
                block_id=hint_block.id,
                source="system",
                hint=action.hint,
                metadata={"hide": True, "internal": True, "reason": "finalization_retry"},
            )

        # ── 阶段 2：准备 messages + tools ────────────────────────────────────────
        # messages 取 memory 的最新快照（hint 写入后已包含在内）。
        # tools 为 None 的两种情况：force_no_tools=True（强制纯文本对话）或
        # tool_view 为空（没有可用工具）；其余情况转为 OpenAI 工具描述列表。
        messages = MessageContext.get_messages(self.agent.state.context, self.agent.system_prompt)
        tools = None if action.force_no_tools else self.agent.tool_view.to_openai_tools() or None

        # 重试上限 = 首次尝试 + 配置的重试次数
        max_attempts = 1 + self.agent.max_retries
        # 轮次起始时间戳，用于计算 TTFT（首 token 延迟）
        turn_start_ts = time.perf_counter()
        # first_token_logged：标记本轮是否已记录过"首 token"时刻。
        # TTFT（Time To First Token）= 从轮次开始到收到第一个流式事件的时间差，
        # 是衡量模型响应延迟的关键指标，只在本轮首次收到事件时记录一次。
        first_token_logged = False

        # 各类增量内容的收集器（跨重试会重置）
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        finish_reason = "unknown"
        usage: dict | None = None
        response_metadata: dict = {}

        # ── 阶段 3：重试循环 ─────────────────────────────────────────────────────
        for attempt in range(max_attempts):
            # 每次尝试单独创建一个 LLM 子 span（若启用了 tracing）
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
                # 通知下游：一次模型调用开始
                yield ModelCallStartEvent(reply_id=reply_id, model_name=model_name)

                # 当前已开启的 block id（用于流式拼接与结束时收尾）；
                # 为 None 表示该类型 block 尚未开始。
                text_block_id: str | None = None
                thinking_block_id: str | None = None
                # 已发过 ToolCallStart 的工具调用 id 集合，避免重复发 Start
                tool_call_started: set[str] = set()

                # B2：BlockAssembler 组装完整块（配对校验在流末 validate）
                assembler = BlockAssembler()

                # ── 阶段 4：流式消费 StreamChunk ───────────────────────────────
                async for chunk in self._stream(
                    messages,
                    tools,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                ):
                    # 首个 chunk 到达时记录 TTFT（仅本轮一次）
                    if not first_token_logged:
                        first_token_logged = True
                        elapsed_ms = (time.perf_counter() - turn_start_ts) * 1000
                        logger.info(f"[react] 第 {self.state.iteration} 轮 TTFT {elapsed_ms:.0f}ms")
                        if llm_span and not llm_span.ended:
                            llm_span.add_event("ttft", {"ms": round(elapsed_ms)})

                    # 协议组装（delta 累积 / 块闭合 / finish 校验）
                    assembler.feed(chunk)

                    # ① 正文文本增量：旁路转发 UI，首次时开启 TextBlock
                    if isinstance(chunk, TextDeltaChunk):
                        text_parts.append(chunk.text)
                        if text_block_id is None:
                            text_block_id = uuid.uuid4().hex[:16]
                            yield TextBlockStartEvent(reply_id=reply_id, block_id=text_block_id)
                        yield TextBlockDeltaEvent(reply_id=reply_id, block_id=text_block_id, delta=chunk.text)

                    # ② 推理文本增量：旁路转发 UI，首次时开启 ThinkingBlock
                    elif isinstance(chunk, ReasoningDeltaChunk):
                        reasoning_parts.append(chunk.text)
                        if thinking_block_id is None:
                            thinking_block_id = uuid.uuid4().hex[:16]
                            yield ThinkingBlockStartEvent(reply_id=reply_id, block_id=thinking_block_id)
                        yield ThinkingBlockDeltaEvent(reply_id=reply_id, block_id=thinking_block_id, delta=chunk.text)

                    # ③ 工具入参增量：旁路转发 UI，首次出现该 call_id 时发 ToolCallStart
                    elif isinstance(chunk, ToolCallDeltaChunk):
                        if chunk.call_id and chunk.call_id not in tool_call_started:
                            tool_call_started.add(chunk.call_id)
                            yield ToolCallStartEvent(reply_id=reply_id, tool_call_id=chunk.call_id, tool_call_name=chunk.name or "")
                        if chunk.call_id:
                            yield ToolCallDeltaEvent(reply_id=reply_id, tool_call_id=chunk.call_id, delta=chunk.arguments_delta)

                    # ④ 块闭合：tool-call 块发 ToolCallEnd 并收录完整调用；
                    #    text / thinking 块关闭对应 UI block
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
                                yield ToolCallStartEvent(reply_id=reply_id, tool_call_id=call_id, tool_call_name=name)
                            yield ToolCallEndEvent(
                                reply_id=reply_id,
                                tool_call_id=call_id,
                                arguments=arguments,
                            )
                            tool_calls.append(ToolCall(id=call_id, name=name, input=parsed))
                        elif block_type == "text":
                            if text_block_id is not None:
                                yield TextBlockEndEvent(reply_id=reply_id, block_id=text_block_id)
                                text_block_id = None
                        elif block_type == "thinking":
                            if thinking_block_id is not None:
                                yield ThinkingBlockEndEvent(reply_id=reply_id, block_id=thinking_block_id)
                                thinking_block_id = None

                    # ⑤ usage：记录（协议保证在 finish 之前）
                    elif isinstance(chunk, UsageChunk):
                        usage = chunk.usage

                    # ⑥ finish：本步结束——error/aborted 还原为异常走重试路径，
                    #    正常 kinds 记录 finish_reason 并发 ModelCallEndEvent
                    elif isinstance(chunk, FinishChunk):
                        reason = chunk.reason
                        if reason.kind in ("error", "aborted"):
                            # 适配器已把 provider 异常收敛为 error finish；
                            # 这里还原为 LLMError 交给统一重试 / 取消处理。
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
                        # 仅接受完整的 OpenAI-compatible usage，避免把缺失字段
                        # 静默补零后覆盖下游最后一次有效调用。
                        if valid_usage:
                            self.state.token_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
                            self.state.token_usage["completion_tokens"] += usage.get("completion_tokens", 0)
                            self.state.token_usage["total_tokens"] += usage.get("total_tokens", 0)
                        else:
                            logger.warning(
                                "LLM 调用未返回完整 token usage，忽略本次用量: "
                                "model=%s required=%s",
                                self.agent.model,
                                sorted(required_usage_fields),
                            )

                        # 收尾仍开启的 block：发对应的 End 事件并清空 id
                        if text_block_id is not None:
                            yield TextBlockEndEvent(reply_id=reply_id, block_id=text_block_id)
                            text_block_id = None
                        if thinking_block_id is not None:
                            yield ThinkingBlockEndEvent(reply_id=reply_id, block_id=thinking_block_id)
                            thinking_block_id = None

                        if valid_usage:
                            yield ModelCallEndEvent(
                                reply_id=reply_id,
                                prompt_tokens=usage["prompt_tokens"],
                                completion_tokens=usage["completion_tokens"],
                                total_tokens=usage["total_tokens"],
                                finished_reason=finish_reason,
                                response_metadata=response_metadata,
                            )

                # ── 阶段 5：成功完成 ───────────────────────────────────────────
                # 流末契约校验：所有 block-start 均已配对 block-end、finish 已收到。
                # 适配器已把 provider 异常收敛为 error/aborted finish 并在循环内
                # raise，走到这里的只有正常 kinds。
                assembler.validate()

                # 拼接本轮完整文本与推理
                full_text = "".join(text_parts)
                full_reasoning = "".join(reasoning_parts)

                # max-tokens 截断：tool-call 的 arguments 是不完整 JSON，无法安全
                # 执行，整体丢弃（对齐 DSH assembled() 的 max-tokens 过滤）；
                # text / reasoning 保留。
                if finish_reason == "max-tokens" and tool_calls:
                    logger.warning(
                        "[react] max-tokens 截断，丢弃 %d 个不完整工具调用: %s",
                        len(tool_calls),
                        [tc.name for tc in tool_calls],
                    )
                    tool_calls = []
                # 关闭 LLM span，输出关键字段供追踪系统记录
                if llm_span and not llm_span.ended:
                    llm_span.end(outputs={
                        "text": full_text,
                        "reasoning": full_reasoning,
                        "finish_reason": finish_reason,
                        "has_tool_calls": bool(tool_calls),
                        "usage": usage,
                        "response_metadata": response_metadata,
                    })

                # 一次 run() 可包含多条 AssistantMsg。当前 Reasoning/Acting 的
                # thinking/text/tool_calls 统一追加到 message_id；只有正式
                # UserMessage 进入下一次 before-reasoning 后才会旋转到新的 id。
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

                # 组装成功的 TurnResult 写入 self.result，结束本轮
                self.result = TurnResult(
                    text=full_text,
                    reasoning=full_reasoning,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    usage=usage,
                )
                return

            # ── 阶段 6a：CancelledError 路径 ───────────────────────────────────
            # 取消是协作式的：先做收尾（关 span、关 block、写半截文本），再 re-raise
            # 让上游感知到取消信号。注意取消不触发重试。
            except asyncio.CancelledError:
                # 以 CANCELLED 状态关闭 span
                if llm_span and not llm_span.ended:
                    llm_span.end(status=TraceRunStatus.CANCELLED)
                # 收尾仍开启的 block，保证下游事件流完整闭合
                if text_block_id is not None:
                    yield TextBlockEndEvent(reply_id=reply_id, block_id=text_block_id)
                    text_block_id = None
                if thinking_block_id is not None:
                    yield ThinkingBlockEndEvent(reply_id=reply_id, block_id=thinking_block_id)
                    thinking_block_id = None
                # 把已生成的半截 thinking/text 追加到当前 reply，尽量保留已有产出。
                _full_text = "".join(text_parts)
                _full_reasoning = "".join(reasoning_parts)
                partial_blocks = []
                if _full_reasoning:
                    partial_blocks.append(ThinkingBlock(thinking=_full_reasoning))
                if _full_text:
                    partial_blocks.append(TextBlock(text=_full_text))
                MessageContext.append_reply_blocks(
                    self.agent.state.context,
                    message_id,
                    partial_blocks,
                )
                raise

            # ── 阶段 6b：其他异常路径 ─────────────────────────────────────────
            # 统一收尾后，按错误是否可重试分流：可重试且未耗尽 → 重试；否则返回 error。
            except Exception as exc:  # noqa: BLE001 - normalize provider failures
                # 以 error 关闭 span
                if llm_span and not llm_span.ended:
                    llm_span.end(error=exc)

                # 收尾仍开启的 block
                if text_block_id is not None:
                    yield TextBlockEndEvent(reply_id=reply_id, block_id=text_block_id)
                    text_block_id = None
                if thinking_block_id is not None:
                    yield ThinkingBlockEndEvent(reply_id=reply_id, block_id=thinking_block_id)
                    thinking_block_id = None

                # 把已生成的半截 thinking/text 追加到当前 reply（非空才写）。
                _full_text = "".join(text_parts)
                _full_reasoning = "".join(reasoning_parts)
                partial_blocks = []
                if _full_reasoning:
                    partial_blocks.append(ThinkingBlock(thinking=_full_reasoning))
                if _full_text:
                    partial_blocks.append(TextBlock(text=_full_text))
                MessageContext.append_reply_blocks(
                    self.agent.state.context,
                    message_id,
                    partial_blocks,
                )

                # 错误归类：已是 LLMError 直接用，否则用 LLMError.classify 推断 code
                err = exc if isinstance(exc, LLMError) else LLMError.classify(exc)
                # 是否已是最后一次尝试
                is_last = attempt >= max_attempts - 1

                # 在 Runtime 默认 retry/stop 分支之前发布一次失败决策 Hook。
                # Hook 只改变“是否继续”的建议；attempt 上限、取消、RetryEvent、
                # 消息重读和半截输出清理仍由本执行器拥有。
                decision = await self._dispatch_llm_error(
                    err,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                )

                logger.warning(
                    "LLM 调用失败 [%s] %s (第 %d/%d 次尝试)",
                    err.code, err.message[:200], attempt + 1, max_attempts,
                )

                # 没有策略 Plugin 时保持原有错误分类；Plugin 可以明确请求 stop，
                # 但任何决定都不能突破最后一次 attempt 或取消屏障。
                should_retry = (
                    err.code not in LLMError.UNRETRYABLE_CODES
                    and not is_last
                )
                if decision is not None:
                    should_retry = decision.action == "retry"
                if is_last or self.state.is_cancelled:
                    should_retry = False

                # 不可重试错误（如鉴权失败、请求非法）或次数耗尽 → 组装带 error 的 TurnResult 返回
                if not should_retry:
                    self.result = TurnResult(
                        text="",
                        reasoning="",
                        tool_calls=[],
                        finish_reason="error",
                        error=err,
                    )
                    return

                # 可重试 → 向下游发 RetryEvent 通知本次失败即将重试
                yield RetryEvent(
                    reply_id=reply_id,
                    code=err.code,
                    message=err.message,
                    attempt=attempt + 1,
                    max_attempts=max_attempts - 1,
                )
                # 退避等待后进入下一轮。Plugin 提供 delay 时只影响本次等待；
                # 未提供时沿用既有 Agent 配置，保持无监听器行为不变。
                delay = self.agent.retry_delay
                if decision is not None and decision.delay is not None:
                    try:
                        delay = max(0.0, float(decision.delay))
                    except (TypeError, ValueError):
                        delay = self.agent.retry_delay
                await asyncio.sleep(delay)
                # 重新读取 messages（memory 可能在等待期间被改动）
                messages = MessageContext.get_messages(self.agent.state.context, self.agent.system_prompt)
                # 重置收集器，避免上一轮的半截内容污染下一轮
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
