"""
CompactManager — 上下文压缩处理器

设计：
- 50% 水位（precompact_threshold）：每轮 LLM 回复结束后后台异步压缩
- 70% 水位（compact_threshold）：用户发消息时阻塞式压缩
- /compact 手动：立即压缩
- /compress-fast：零 LLM 成本裁剪旧 ToolResultBlock 输出

每次压缩：从上一个 compact 摘要 Msg 到现在，全量 LLM 摘要。摘要作为一条
role=user、name=compact 的 Msg 追加到 messages 数组（由 SessionProjection 投影
context_compact_done 落盘），原始 Msg 永不删除。下一轮 LLM 上下文从最后一条
compact Msg 开始。CompactManager 不直接写 state、不直接派发 WebSocket，全部
通过 CustomEvent + 统一事件出口完成。快速压缩直接更新旧 Msg 中的工具结果块。

并发安全：
- 每个 session 同一时间最多只有一个真正的压缩 Task。
- 后来的手动、关键路径或 idle 压缩请求不创建新任务，统一等待已有 Task。
- 等待者取消不会中断共享压缩；只有 Gateway 关闭时才强制取消。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

from ftre_agent_core.event import CustomEvent
from ftre_agent_core.llm import LLMError, LLMHandler, TextDelta
from ftre_agent_core.message import Msg, MsgName, TextBlock, ToolResultBlock

from .compact_events import CompactEventName

logger = logging.getLogger(__name__)

DEFAULT_PRECOMPACT_THRESHOLD = 0.5
DEFAULT_COMPACT_THRESHOLD = 0.7

# compress-fast 默认保留最近 N 个工具结果完整
DEFAULT_FAST_KEEP_RECENT = 3


# 不可重试的 LLM 错误码 → 触发冷却退避
COMPACT_UNRETRYABLE_LLM_CODES = {"auth_error", "bad_request", "content_filter"}
COMPACT_UNRETRYABLE_COOLDOWN_SECONDS = 300

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
        self._last_llm_errors: dict[str, LLMError | None] = {}

        # session_id → 真正执行 _do_compact 的共享 Task。
        # 这里只保存压缩本体，不保存后台调度的包装 Task，避免任务取消自己。
        self._compact_tasks: dict[str, asyncio.Task[str | None]] = {}
        self._compact_retry_after: dict[str, float] = {}

    # ─── 只读判断 ──────────────────────────────────────────────────

    async def should_compact(
        self, session_id: str, channel_id: str, config, *, threshold: float | None = None
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
        usage = await self.session_manager.get_token_usage(session_id)
        estimated = usage["total"]
        if estimated <= 0:
            return False
        return (estimated / cw) >= threshold

    # ─── 异步执行压缩 ──────────────────────────────────────────────

    async def compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        trigger: Literal["auto", "manual", "idle"] = "auto",
        preserve_from_message_id: str = "",
    ) -> str | None:
        """执行或等待该 session 当前唯一的压缩任务。

        创建 Task 与写入字典之间没有 ``await``，在 asyncio 单线程事件循环中
        是原子的。``shield`` 保证某个等待者被取消时，共享压缩仍继续运行。
        """
        task, created = self._get_or_create_compact_task(
            session_id,
            channel_id,
            config=config,
            trigger=trigger,
            preserve_from_message_id=preserve_from_message_id,
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
        trigger: Literal["auto", "manual", "idle"],
        preserve_from_message_id: str = "",
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
                preserve_from_message_id=preserve_from_message_id,
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
        keep_recent: int = DEFAULT_FAST_KEEP_RECENT,
    ) -> bool:
        """快速压缩：不调 LLM，直接裁剪旧 ToolResultBlock 输出。

        策略：保留最近 keep_recent 个工具结果完整，其余结果原位替换成
        ``[工具输出已压缩]``，不会写入任何流式事件或兼容标记。

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
        tool_results: list[tuple[Msg, ToolResultBlock]] = []
        for message in messages:
            for block in message.content:
                if (
                    isinstance(block, ToolResultBlock)
                    and _tool_result_text(block) != "[工具输出已压缩]"
                ):
                    tool_results.append((message, block))

        if len(tool_results) <= keep_recent:
            logger.info(
                f"[compact-fast] session={session_id} 工具结果数 "
                f"{len(tool_results)} <= keep_recent={keep_recent}，跳过"
            )
            return False

        to_compact = (
            tool_results[:-keep_recent]
            if keep_recent > 0
            else tool_results
        )

        # 估算压缩前后 token（只算活跃区间）
        from ftre.session.message.token_counter import estimate_messages_tokens
        tokens_before = estimate_messages_tokens(active_records)
        changed_messages: dict[str, Msg] = {}
        for message, block in to_compact:
            block.output = [TextBlock(text="[工具输出已压缩]")]
            changed_messages[message.id] = message
        tokens_after = estimate_messages_tokens(messages)

        try:
            for message in changed_messages.values():
                await self.session_manager.update_message(message)
        except Exception:
            logger.exception(f"[compact-fast] 更新 Msg 失败 session={session_id}")
            return False

        # 通知前端（fast 模式不投影为 Msg，仅广播）
        await self._emit_event(session_id, channel_id, CustomEvent(
            name=CompactEventName.DONE,
            value={
                "mode": "fast",
                "messages": len(changed_messages),
                "tool_results": len(to_compact),
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            },
        ))

        logger.info(
            f"[compact-fast] session={session_id} 裁剪 {len(to_compact)} 个工具结果, "
            f"tokens {tokens_before} → {tokens_after}"
        )
        return True

    async def _do_compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        trigger: Literal["auto", "manual", "idle"] = "auto",
        preserve_from_message_id: str = "",
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

        # 关键路径压缩发生在本轮 UserMsg 已经可靠落盘之后。当前输入必须留在
        # compact Msg 后面的 tail，不能被摘要吞掉；它之后若有并发新增消息也一并保留。
        if preserve_from_message_id:
            preserve_index = next(
                (
                    index
                    for index, record in enumerate(head_messages)
                    if record.get("id") == preserve_from_message_id
                ),
                -1,
            )
            if preserve_index >= 0:
                head_messages = head_messages[:preserve_index]
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

        # 4. 通知前端开始（CustomEvent，不持久化）
        await self._emit_event(session_id, channel_id, CustomEvent(
            name=CompactEventName.START,
            value={
                "messages": len(head_messages),
                "tokens": tokens_before,
            },
        ))

        # 5. LLM 直调摘要（previous_summary 参与滚动摘要）
        summary = await self._run_compact_llm(
            head_messages, config=config, previous_summary=previous_summary,
            session_id=session_id,
        )
        if not summary:
            logger.warning(f"[compact] session={session_id} LLM 摘要失败")
            await self._emit_failed(session_id, channel_id, "LLM 摘要未产出合格结果")
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
    ) -> str | None:
        """调用 LLM 生成摘要（异步）。

        采用 OpenCode 的 serialize → select → buildPrompt 模式：
        1. 序列化 Msg 为纯文本（截断 tool 输出至 2000 字符）
        2. buildPrompt() 返回多条 user message：对话记录 + 指令/模板
        3. 多条 user message 依次发给 LLM
        """
        self._last_llm_errors[session_id] = None
        try:
            context = _serialize_messages(head_messages)
            if not context.strip():
                logger.debug("[compact] 消息文本为空，跳过 LLM 调用")
                return None

            prompt_parts = _build_prompt(
                previous_summary=previous_summary,
                context=[context],
                min_chars=max(200, int(_estimate_body_chars(context) * 0.6)),
            )

            messages = [
                {"role": "system", "content": COMPACT_LLM_SYSTEM_PROMPT},
                *[{"role": "user", "content": p} for p in prompt_parts],
            ]

            # 优先使用 compact_llm，未配置则回退到主 llm
            llm_cfg = getattr(config, "compact_llm", None) or config.llm
            handler = LLMHandler(
                model=llm_cfg.model,
                api_key=llm_cfg.api_key,
                api_base=llm_cfg.api_base,
                api_type=llm_cfg.api_type,
                reasoning_effort=llm_cfg.reasoning_effort,
                temperature=0.0,
            )

            collected: list[str] = []
            async for ev in handler.stream(messages):
                if isinstance(ev, TextDelta):
                    collected.append(ev.text)

            summary = "".join(collected).strip()
            if not summary:
                logger.warning(f"[compact] LLM 摘要为空")
                return None
            return summary
        except LLMError as exc:
            self._last_llm_errors[session_id] = exc
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

    # ─── 后台 idle 压缩调度 ───────────────────────────────────────

    async def maybe_schedule_idle_compact(
        self, session_id: str, channel_id: str, config,
    ) -> None:
        """主事件循环里：水位 ≥ threshold → 后台压缩。

        去重：同一 session 同一时间只允许一个后台 compact task 在飞。
        如果上一个还没完成就不再派发，避免 cron session 连续触发导致反复压缩。
        """
        try:
            if not getattr(config.context, "idle_compaction", True):
                return

            retry_after = self._compact_retry_after.get(session_id)
            now = time.monotonic()
            if retry_after is not None and now < retry_after:
                logger.debug(
                    "[compact] session=%s 后台压缩冷却中，%.0fs 后重试",
                    session_id,
                    retry_after - now,
                )
                return
            if retry_after is not None:
                self._compact_retry_after.pop(session_id, None)

            need = await self.should_compact(
                session_id,
                channel_id,
                config,
                threshold=getattr(config.context, "precompact_threshold", DEFAULT_PRECOMPACT_THRESHOLD),
            )
            if not need:
                return

            # 同一 session 已有压缩任务时无需再派发后台包装任务。
            if self.is_compacting(session_id):
                logger.debug(f"[compact] session={session_id} 已有后台压缩在飞，跳过")
                return

            # 先同步登记真正的压缩 Task，再返回调度函数。这样消息入口从此刻
            # 起就能可靠观察到 is_compacting=True，没有包装协程启动窗口。
            task, created = self._get_or_create_compact_task(
                session_id,
                channel_id,
                config=config,
                trigger="idle",
            )
            if not created:
                return

            # 后台压缩监视器只负责冷却策略，不参与单任务登记。
            async def _do_bg_compact():
                try:
                    summary = await asyncio.shield(task)
                    llm_error = self._last_llm_errors.get(session_id)
                    if summary is not None:
                        self._compact_retry_after.pop(session_id, None)
                    elif (
                        llm_error is not None
                        and getattr(llm_error, "code", None) in COMPACT_UNRETRYABLE_LLM_CODES
                    ):
                        self._compact_retry_after[session_id] = (
                            time.monotonic() + COMPACT_UNRETRYABLE_COOLDOWN_SECONDS
                        )
                        logger.warning(
                            "[compact] session=%s 后台压缩遇到不可重试 LLM 错误 code=%s，冷却 %ss",
                            session_id,
                            llm_error.code,
                            COMPACT_UNRETRYABLE_COOLDOWN_SECONDS,
                        )
                except Exception:
                    logger.exception(
                        "[compact] idle 后台压缩异常 session=%s",
                        session_id,
                    )

            asyncio.create_task(_do_bg_compact())
            logger.info(f"[compact] idle 后台压缩已派发 session={session_id}")
        except Exception:
            logger.exception(f"[compact] idle 压缩调度异常 session={session_id}")

    def cancel_all_compact_tasks(self) -> None:
        """stop() 时调用，取消所有在飞的后台压缩 task。"""
        for task in self._compact_tasks.values():
            task.cancel()
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
) -> list[str]:
    """构建 LLM 摘要的 user messages（多条）。

    返回多条 user message 内容，结构：
    - 对话记录（每段一个 <conversation> 块）
    - （可选）上一次摘要 <previous-summary>
    - 最后一条：指令（XML 模板已在 system prompt 中）

    首次压缩：Create a new anchored summary from the conversation history.
    增量压缩：Update the anchored summary below using the conversation history above.
              Preserve still-true details, remove stale details, and merge in the new facts.
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
