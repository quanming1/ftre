"""
CompactManager — 上下文压缩处理器

设计：
- SessionLane 在领取下一条请求前做强制水位检查并等待压缩
- /compact 手动：立即压缩
- /compress-fast：零 LLM 成本裁剪旧 ToolResultBlock 输出

每次压缩：从上一个 compact 摘要 Msg 到现在，全量 LLM 摘要。摘要作为一条
role=user、name=compact 的 Msg 追加到 messages 数组（由 SessionProjection 投影
context_compact_done 落盘），原始 Msg 永不删除。下一轮 LLM 上下文从最后一条
compact Msg 开始。CompactManager 不直接写 state、不直接派发 WebSocket，全部
通过 CustomEvent + 统一事件出口完成。快速压缩直接更新旧 Msg 中的工具结果块。

并发安全：
- 每个 session 同一时间最多只有一个真正的压缩 Task。
- 后来的手动或关键路径压缩请求不创建新任务，统一等待已有 Task。
- 等待者取消不会中断共享压缩；只有 Gateway 关闭时才强制取消。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from ftre_agent_core.event import CustomEvent
from ftre_agent_core.llm import LLMError, TextDeltaChunk, create_llm_handler
from ftre_agent_core.message import Msg, MsgName, TextBlock, ToolResultBlock

from .compact_events import CompactEventName

logger = logging.getLogger(__name__)

DEFAULT_COMPACT_THRESHOLD = 0.8

# compress-fast 默认保护最近 N 轮对话内的工具输出不被裁剪（0 = 全裁）
DEFAULT_FAST_KEEP_TURNS = 0


# LLM 摘要的 system prompt
COMPACT_LLM_SYSTEM_PROMPT = """\
你是对话上下文压缩组件。当对话的上下文窗口即将溢出时，由你负责生成摘要。你产出的摘要将成为 Agent 在此之前所有记忆的唯一来源。Agent 将仅依据此摘要（以及少量恢复的文件/图片附件）恢复工作。

首先，将你的推理过程包裹在 <analysis> 块中。在其中按时间线梳理整段对话，逐节识别：用户的明确请求与意图、你处理这些请求的方式、关键决策/技术概念/代码模式、具体细节（文件名、代码片段、函数签名、文件编辑）、遇到的错误及其修复方式、以及用户的任何特定反馈——尤其是用户要求你换种方式做事时。<analysis> 块在摘要传递给下一个 Agent 之前会被剥离；它纯粹是用于提升后续摘要质量的草稿区。

然后，按照下方 EXACT XML 结构输出最终摘要。内容要密集。省略对话性填充。

<state_snapshot>
    <primary_request_and_intent>
        <!-- 详细记录用户的所有明确请求和意图。在意图存在歧义时引用用户的原话。 -->
    </primary_request_and_intent>

    <key_technical_concepts>
        <!-- 列出所有涉及的重要技术概念、技术和框架。 -->
    </key_technical_concepts>

    <files_and_code_sections>
        <!-- 逐一列出检查、修改或创建的文件和代码段。特别关注最近的消息。在适用处包含完整代码片段，并说明该文件读取或编辑为何重要。 -->
    </files_and_code_sections>

    <errors_and_fixes>
        <!-- 列出每个遇到的错误及其修复方式。包含被引用给 Agent 的原始错误消息。特别关注用户对错误的反馈，尤其是用户要求你换种方式处理时。 -->
    </errors_and_fixes>

    <problem_solving>
        <!-- 记录已解决的问题和任何正在进行的排障工作。 -->
    </problem_solving>

    <all_user_messages>
        <!-- 按时间顺序列出所有非工具结果的用户消息。这些对理解用户反馈和意图变化至关重要。包含 "ok"、"continue" 等短消息——它们是信号。 -->
    </all_user_messages>

    <pending_tasks>
        <!-- 列出用户已明确要求但尚未完成的待办任务。 -->
    </pending_tasks>

    <current_work>
        <!-- 详细描述在请求摘要之前 Agent 正在做什么，特别关注用户和助手的最近消息。在适用处包含文件名和代码片段。 -->
    </current_work>

    <next_step>
        <!-- 列出与最近工作相关的唯一下一步。该步骤必须与用户最近的明确请求和请求摘要前 Agent 正在做的任务直接对齐。如果上一个任务已结束，仅在直接符合用户请求时才列出下一步——不要在未与用户确认前开始旁支或旧的工作。如果有下一步，包含最近对话中的直接引用，准确展示你当时在做什么、停在哪里。 -->
    </next_step>
</state_snapshot>"""


def _select_compact_llm(config):
    """返回本次摘要真实使用的 LLM 配置。"""
    return getattr(config, "compact_llm", None) or config.llm


class CompactManager:
    """上下文压缩处理器（全异步）。"""

    def __init__(
        self,
        *,
        session_manager,
        emit_event,
        threshold: float = DEFAULT_COMPACT_THRESHOLD,
    ):
        self.session_manager = session_manager
        self._emit_event = emit_event
        self._threshold = threshold

        # session_id → 真正执行 _do_compact 的共享 Task。
        # 这里只保存压缩本体，不保存后台调度的包装 Task，避免任务取消自己。
        self._compact_tasks: dict[str, asyncio.Task[str | None]] = {}

    # ─── 只读判断 ──────────────────────────────────────────────────

    async def should_compact(
        self,
        session_id: str,
        channel_id: str,
        config,
        *,
        threshold: float | None = None,
        extra_tokens: int = 0,
    ) -> bool:
        """水位是否超过 threshold？只读 DB，不调 LLM。

        优先用 API 报告的真实 token（last_call_usage + pending 策略），
        全新 session 无 last_call_usage 时退化为字符估算。
        """
        threshold = threshold if threshold is not None else getattr(
            config.context, "compact_threshold", self._threshold
        )
        cw = getattr(config.llm, "context_window", None)
        if not cw or cw <= 0:
            return False
        max_output = max(0, getattr(config.llm, "max_output", None) or 0)
        safety_buffer = max(
            0, getattr(config.context, "safety_buffer", 0) or 0
        )
        prompt_budget = cw - max_output - safety_buffer
        if prompt_budget <= 0:
            # 配置无效或不安全：输出预算和安全余量已经占满上下文窗口，
            # 任意非空提示词都应视为超过可用预算。
            return True
        usage = await self.session_manager.get_token_usage(session_id)
        estimated = usage["total"] + max(0, extra_tokens)
        if estimated <= 0:
            return False
        return (estimated / prompt_budget) >= threshold

    # ─── 异步执行压缩 ──────────────────────────────────────────────

    async def compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        trigger: Literal["auto", "manual"] = "auto",
        focus_hint: str = "",
    ) -> str | None:
        """执行或等待该 session 当前唯一的压缩任务。

        创建 Task 与写入字典之间没有 ``await``，在 asyncio 单线程事件循环中
        是原子的。``shield`` 保证某个等待者被取消时，共享压缩仍继续运行。

        ``focus_hint`` 为用户在 ``/compact`` 后附带的自然语言提示词，透传给
        摘要 LLM，强调必须优先完整保留的上下文（如「登录模块相关代码」）。
        """
        task, created = self._get_or_create_compact_task(
            session_id,
            channel_id,
            config=config,
            trigger=trigger,
            focus_hint=focus_hint,
        )
        if not created:
            logger.info(
                "[compact] session=%s 已有压缩任务，等待其完成 trigger=%s",
                session_id,
                trigger,
            )
        return await asyncio.shield(task)

    def _get_or_create_compact_task(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        trigger: Literal["auto", "manual"],
        focus_hint: str = "",
    ) -> tuple[asyncio.Task[str | None], bool]:
        """同步取得或创建共享压缩 Task；返回 ``(task, 是否新建)``。"""
        existing = self._compact_tasks.get(session_id)
        if existing is not None and not existing.done():
            return existing, False

        task = asyncio.create_task(
            self._do_compact(
                session_id,
                channel_id,
                config=config,
                trigger=trigger,
                focus_hint=focus_hint,
            )
        )
        self._compact_tasks[session_id] = task

        def _cleanup(done: asyncio.Task[str | None]) -> None:
            # 只清理由自己登记的条目，避免旧 Task 的回调误删后继任务。
            if self._compact_tasks.get(session_id) is done:
                self._compact_tasks.pop(session_id, None)

        task.add_done_callback(_cleanup)
        return task, True

    def is_compacting(self, session_id: str) -> bool:
        """该 session 是否存在尚未完成的共享压缩任务。"""
        task = self._compact_tasks.get(session_id)
        return task is not None and not task.done()

    # ─── 快速压缩（零 LLM 成本） ───────────────────────────────────

    async def compress_fast(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        keep_turns: int = DEFAULT_FAST_KEEP_TURNS,
    ) -> bool:
        """快速压缩：不调 LLM，直接裁剪旧 ToolResultBlock 输出。

        策略：按 turn 边界（一条 role=user 的 Msg 开启一轮）保护最近
        ``keep_turns`` 轮对话，这些轮内的工具输出全部完整保留；更早的
        ToolResultBlock 原位替换成 ``[工具输出已压缩]``，不写入任何流式
        事件或兼容标记。``keep_turns=0`` 表示裁剪活跃区间内的全部工具输出。
        裁剪同时作废活跃区间内的 token usage 锚点（见函数内注释），防止
        水位冻结导致 should_compact 永远判过线。

        Returns:
            True: 执行了裁剪
            False: 没有工具结果可裁剪
        """
        # 活跃区间 = 最后一条 compact Msg 之后的 tail（无 compact 则全量）
        context_records = await self.session_manager.get_context_messages(session_id)
        if not context_records:
            return False
        # get_context_messages 返回 [last_compact?, *tail]；去掉 leading compact
        active_records = context_records
        if context_records[0].get("name") == MsgName.COMPACT:
            active_records = context_records[1:]
        if not active_records:
            return False
        messages = [Msg.model_validate(record) for record in active_records]

        # 按 turn 边界确定保护区：从后往前数 keep_turns 个 role=user 的 Msg，
        # 该 Msg 及其之后的所有 Msg 属于受保护轮，其中的工具输出不裁剪。
        # compact_fast 气泡是 role=assistant，天然不参与用户轮计数，无需特殊排除。
        protected_start = len(messages)  # 默认 keep_turns=0 → 无保护，全部可裁
        if keep_turns > 0:
            seen_turns = 0
            for index in range(len(messages) - 1, -1, -1):
                if messages[index].role == "user":
                    seen_turns += 1
                    protected_start = index
                    if seen_turns >= keep_turns:
                        break

        tool_results: list[tuple[Msg, ToolResultBlock]] = []
        for index, message in enumerate(messages):
            if index >= protected_start:
                break  # 受保护轮内的工具输出全部保留
            for block in message.content:
                if (
                    isinstance(block, ToolResultBlock)
                    and _tool_result_text(block) != "[工具输出已压缩]"
                ):
                    tool_results.append((message, block))

        if not tool_results:
            logger.info(
                f"[compact-fast] session={session_id} 无可裁剪工具结果"
                f"（keep_turns={keep_turns}），跳过"
            )
            return False

        # 估算压缩前后 token（只算活跃区间）
        from ftre.session.message.token_counter import estimate_messages_tokens
        tokens_before = estimate_messages_tokens(active_records)
        changed_messages: dict[str, Msg] = {}
        for message, block in tool_results:
            block.output = [TextBlock(text="[工具输出已压缩]")]
            changed_messages[message.id] = message
        # 作废活跃区间内所有 last_call_usage 锚点：这些 usage 实算时 prompt 还
        # 包含已被裁掉的工具输出，保留它们会让 get_token_usage 的水位冻结在
        # 裁剪前的值——should_compact 永远判过线，Lane 直接 BLOCKED 死锁。
        # 作废后统计退化为对裁剪后上下文的字符估算；下一次真实 LLM 调用会
        # 写入新的 usage 锚点。
        for message in messages:
            if message.token is not None:
                message.token = None
                changed_messages[message.id] = message
        tokens_after = estimate_messages_tokens(messages)

        try:
            await self.session_manager.update_messages(list(changed_messages.values()))
        except Exception:
            logger.exception(f"[compact-fast] 更新 Msg 失败 session={session_id}")
            return False

        # 通知前端（fast 模式投影为一条 compact_fast 展示气泡 Msg）
        await self._emit_event(session_id, channel_id, CustomEvent(
            name=CompactEventName.DONE,
            value={
                "mode": "fast",
                "messages": len(changed_messages),
                "tool_results": len(tool_results),
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            },
        ))

        logger.info(
            f"[compact-fast] session={session_id} 裁剪 {len(tool_results)} 个工具结果, "
            f"tokens {tokens_before} → {tokens_after}"
        )
        return True

    async def _do_compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        trigger: Literal["auto", "manual"] = "auto",
        focus_hint: str = "",
    ) -> str | None:
        """压缩主逻辑：读 Msg → LLM 摘要 → 发 context_compact_done 投影为 Msg。

        摘要 Msg 的持久化由 SessionProjection 完成，本方法不直接写 state。
        """

        # 1. 读取模型上下文（最后一条 compact + tail）
        context_records = await self.session_manager.get_context_messages(session_id)
        if not context_records:
            logger.info(f"[compact] session={session_id} 无消息，跳过")
            await self._emit_failed(session_id, channel_id, "当前会话没有历史消息")
            return None

        # 2. 分离 leading compact 摘要与 tail；tail 是本次要压缩的真实 Msg
        previous_summary: str | None = None
        head_messages = context_records
        first = context_records[0]
        first_name = first.get("name")
        if first_name == MsgName.COMPACT:
            previous_summary = Msg.model_validate(first).get_text_content() or None
            head_messages = context_records[1:]

        if not head_messages:
            logger.info(f"[compact] session={session_id} 摘要游标后无新消息，跳过")
            return None
        # 本次压缩覆盖到的最后一条真实 Msg（compact 期间新增的消息
        # 不在其中，会自然留在新摘要 Msg 之后）
        through_message_id = head_messages[-1]["id"]

        # 3. 估算当前 token（优先用 API 真实值）
        cw = getattr(config.llm, "context_window", None)
        if not cw or cw <= 0:
            logger.info(f"[compact] session={session_id} context_window={cw} 无效，跳过")
            return None
        usage = await self.session_manager.get_token_usage(session_id)
        tokens_before = usage["total"]
        current_ratio = tokens_before / cw

        # 4. 通知前端开始（CustomEvent，不持久化）。模型必须显式随事件传递，
        #    不能让客户端从上一条 assistant 回复推断压缩实际使用的模型。
        compact_llm = _select_compact_llm(config)
        await self._emit_event(session_id, channel_id, CustomEvent(
            name=CompactEventName.START,
            value={
                "messages": len(head_messages),
                "tokens": tokens_before,
                "model": compact_llm.model,
            },
        ))

        # 5. LLM 直调摘要（previous_summary 参与滚动摘要）
        summary = await self._run_compact_llm(
            head_messages, config=config, previous_summary=previous_summary,
            session_id=session_id, focus_hint=focus_hint,
        )
        if not summary:
            # 摘要为空（模型只输出思考、接口异常等）：默认重试一次
            logger.warning(f"[compact] session={session_id} 摘要为空，重试一次")
            summary = await self._run_compact_llm(
                head_messages, config=config, previous_summary=previous_summary,
                session_id=session_id, focus_hint=focus_hint,
            )
        if not summary:
            # 重试仍失败：回退 compress_fast 兜底，避免直接放弃导致 Lane BLOCKED。
            # （auto 路径的 ContextGate 也会再跑一次 compress_fast，但那是幂等
            # 无操作：可裁的工具输出已在这次被裁完。）
            logger.warning(
                f"[compact] session={session_id} 重试后摘要仍为空，回退 compress_fast"
            )
            await self._emit_failed(
                session_id, channel_id, "LLM 摘要未产出正文，已回退快速压缩",
            )
            try:
                await self.compress_fast(session_id, channel_id, config=config)
            except Exception:
                logger.exception(
                    f"[compact] session={session_id} compress_fast 回退执行失败"
                )
            return None

        # 6. 估算摘要后 token
        from ftre.session.message.token_counter import estimate_messages_tokens
        tokens_after = estimate_messages_tokens([{
            "role": "user",
            "content": [{"type": "text", "text": summary}],
        }])

        # 膨胀保护：摘要比原文还大 → 放弃
        if tokens_after >= tokens_before:
            logger.warning(
                f"[compact] session={session_id} 摘要膨胀 {tokens_before} → {tokens_after}，放弃"
            )
            await self._emit_failed(
                session_id, channel_id, "压缩后体积未减小",
            )
            return None

        # 7. 发 context_compact_done：SessionProjection 将其投影为 user/compact Msg
        #    value 含完整 summary_text（非预览），持久化由 Projection 完成。
        done_event = CustomEvent(
            name=CompactEventName.DONE,
            value={
                "summary_text": summary,
                "through_message_id": through_message_id,
                "trigger": trigger,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "mode": "summary",
            },
        )
        await self._emit_event(session_id, channel_id, done_event)

        logger.info(
            f"[compact] session={session_id} 压缩完成 "
            f"messages={len(head_messages)}，摘要 {len(summary)} 字符"
        )
        return summary

    # ─── LLM 直调摘要 ──────────────────────────────────────────────

    async def _run_compact_llm(
        self,
        head_messages: list[dict],
        *,
        config,
        previous_summary: str | None = None,
        session_id: str = "",
        focus_hint: str = "",
    ) -> str | None:
        """调用 LLM 生成摘要（异步）。

        采用 OpenCode 的 serialize → select → buildPrompt 模式：
        1. 序列化 Msg 为纯文本（截断 tool 输出至 2000 字符）
        2. buildPrompt() 返回多条 user message：对话记录 + 指令/模板
        3. 多条 user message 依次发给 LLM
        """
        try:
            context = _serialize_messages(head_messages)
            if not context.strip():
                logger.debug("[compact] 消息文本为空，跳过 LLM 调用")
                return None

            prompt_parts = _build_prompt(
                previous_summary=previous_summary,
                context=[context],
                min_chars=max(200, int(_estimate_body_chars(context) * 0.6)),
                focus_hint=focus_hint,
            )

            messages = [
                {"role": "system", "content": COMPACT_LLM_SYSTEM_PROMPT},
                *[{"role": "user", "content": p} for p in prompt_parts],
            ]

            # 优先使用 compact_llm，未配置则回退到主 llm。
            # 与 START 事件使用同一选择函数，确保展示模型与真实调用一致。
            llm_cfg = _select_compact_llm(config)
            adapter = create_llm_handler(
                llm_cfg.api_type,
                model=llm_cfg.model,
                api_key=llm_cfg.api_key,
                api_base=llm_cfg.api_base,
                reasoning_effort=llm_cfg.reasoning_effort,
                temperature=0.0,
            )

            collected: list[str] = []
            async for chunk in adapter.stream(messages):
                if isinstance(chunk, TextDeltaChunk):
                    collected.append(chunk.text)

            summary = "".join(collected).strip()
            if not summary:
                logger.warning(f"[compact] LLM 摘要为空")
                return None
            return summary
        except LLMError as exc:
            logger.warning("[compact] LLM 直调摘要失败 code=%s message=%s", exc.code, exc.message)
            return None
        except Exception:
            logger.exception("[compact] LLM 直调摘要异常")
            return None
    # ─── 工具方法 ──────────────────────────────────────────────────

    async def _emit_failed(self, session_id: str, channel_id: str, reason: str) -> None:
        """发 context_compact_failed CustomEvent（不持久化）。"""
        try:
            await self._emit_event(session_id, channel_id, CustomEvent(
                name=CompactEventName.FAILED,
                value={"reason": reason},
            ))
        except Exception:
            logger.debug(f"[compact] 通知失败失败: {reason}")

    async def cancel_compact(self, session_id: str) -> bool:
        """取消并等待指定 session 的真实压缩 Task 完全退出。"""
        task = self._compact_tasks.get(session_id)
        if task is None:
            return False
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._compact_tasks.get(session_id) is task:
            self._compact_tasks.pop(session_id, None)
        return True

    async def cancel_all_compact_tasks(self) -> None:
        """Gateway stop 时取消并等待所有真实压缩 Task。"""
        tasks = list(self._compact_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._compact_tasks.clear()


# ─── 模块级纯函数（可单测） ───────────────────────────────────────────


def _serialize_messages(
    chunk: list[dict],
    *,
    tool_output_max_chars: int = 2000,
) -> str:
    """把 Msg 历史序列化为 LLM 可读的纯文本。"""
    from ftre.session.message.converter import to_openai

    def content_text(content, *, include_thinking: bool = False) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                text_parts.append(str(part.get("text", "")))
            elif include_thinking and part_type == "thinking":
                text_parts.append(str(part.get("thinking", "")))
        return "\n".join(part for part in text_parts if part)

    tool_states: dict[str, str] = {}
    for record in chunk:
        message = Msg.model_validate(record)
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                tool_states[block.id] = str(block.state)

    parts: list[str] = []
    for message in to_openai(chunk, config={"llm": {"vision": False}}):
        role = message.get("role")
        if role == "user":
            text = content_text(message.get("content"), include_thinking=True)
            if text:
                parts.append(f"[User]: {text}")
            continue

        if role == "assistant":
            reasoning = str(message.get("reasoning_content") or "")
            if reasoning:
                parts.append(f"[Assistant reasoning]: {reasoning}")
            text = content_text(message.get("content"))
            if text:
                parts.append(f"[Assistant]: {text}")
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                parts.append(
                    f"[Assistant tool call]: "
                    f"{function.get('name', '?')}({function.get('arguments', '{}')})"
                )
            continue

        if role == "tool":
            tool_call_id = str(message.get("tool_call_id", ""))
            result = content_text(message.get("content"), include_thinking=True)
            if len(result) > tool_output_max_chars:
                result = result[:tool_output_max_chars] + "\n[truncated]"
            label = (
                "Tool error"
                if tool_states.get(tool_call_id) in {"error", "interrupted", "denied"}
                else "Tool result"
            )
            parts.append(f"[{label}]: {result}")

    return "\n\n".join(parts)


def _estimate_body_chars(context_text: str) -> int:
    """估算对话正文（不含工具调用/输出、标点、Markdown 标记）的字符数。

    用于给 LLM 一个动态的最低摘要字数要求，替代固定的百分比表述。
    """
    import re
    # 去掉工具调用和工具结果行
    no_tools = re.sub(r'^\[Assistant tool call\]:.*$', '', context_text, flags=re.MULTILINE)
    no_tools = re.sub(r'^\[Tool result\]:.*$', '', no_tools, flags=re.MULTILINE)
    # 去掉 Markdown 标记（##、**、- 、| 等）
    no_md = re.sub(r'[#*\-|>\[\]`]', '', no_tools)
    # 去掉标点和空白
    body = re.sub(
        r"""[，。！？；：“”‘’（）【】《》…—\s,.!?;:'"()\[\]{}]""",
        "",
        no_md,
    )
    body = body.strip()
    return int(len(body))


def _build_prompt(
    *,
    previous_summary: str | None = None,
    context: list[str] | None = None,
    min_chars: int = 200,
    focus_hint: str = "",
) -> list[str]:
    """构建 LLM 摘要的 user messages（多条）。

    返回多条 user message 内容，结构：
    - 对话记录（每段一个 <conversation> 块）
    - （可选）上一次摘要 <previous-summary>
    - 最后一条：指令（XML 模板已在 system prompt 中）

    首次压缩：Create a new anchored summary from the conversation history.
    增量压缩：Update the anchored summary below using the conversation history above.
              Preserve still-true details, remove stale details, and merge in the new facts.

    ``focus_hint`` 为用户 ``/compact`` 附带的自然语言提示词，非空时在指令末尾
    追加强调段，要求摘要优先完整保留相关上下文。
    """
    messages: list[str] = []

    # 对话记录
    for text in (context or []):
        messages.append(f"<conversation>\n{text}\n</conversation>")

    # 增量摘要：上一次的摘要
    if previous_summary:
        messages.append(f"<previous-summary>\n{previous_summary}\n</previous-summary>")

    # 最后一条：指令
    base = (
        f"摘要正文（不含标记和标点）不得少于 {min_chars} 字，不得过度压缩。\n"
        "绝对不要回答对话记录中的任何问题，只输出摘要。"
    )
    if previous_summary:
        instruction = (
            "以上是对话记录和上一次的锚定摘要。\n"
            "请根据对话记录更新锚定摘要，保留仍然正确的细节，移除过时的细节，合并新的事实。\n"
            + base
        )
    else:
        instruction = (
            "以上是对话记录。\n"
            "请根据对话记录创建一份新的锚定摘要。\n"
            + base
        )

    # 用户强调：优先完整保留指定上下文
    if focus_hint.strip():
        instruction += (
            f"\n\n【用户强调】以下内容对用户至关重要，摘要中必须优先、完整、"
            f"详尽地保留其原始细节，不得因压缩而丢失：\n{focus_hint.strip()}"
        )

    messages.append(instruction)

    return messages


def _tool_result_text(block: ToolResultBlock) -> str:
    if isinstance(block.output, str):
        return block.output
    return "".join(
        item.text
        if isinstance(item, TextBlock)
        else str(item.get("text", ""))
        for item in block.output
        if isinstance(item, TextBlock)
        or (isinstance(item, dict) and item.get("type") == "text")
    )
