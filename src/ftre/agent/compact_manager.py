"""
CompactManager — 上下文压缩处理器

设计：
- 50% 水位（precompact_threshold）：每轮 LLM 回复结束后后台异步压缩
- 70% 水位（compact_threshold）：用户发消息时阻塞式压缩
- /compact 手动：立即压缩
- /compress-fast：零 LLM 成本裁剪旧 tool_result

每次压缩：从上一个 context_compact(summary) 到现在，全量 LLM 摘要，
compact event 放末尾（timestamp=触发时间）。写入即生效，无 pending。

to_openai_messages 遍历 context_compact 事件：
- mode=fast → 不清空，靠 compacted_ids 标记 tool_result 为占位符
- mode=summary → 清空之前所有 messages + 注入 summary

并发安全：
- compact() 总是在 AgentLoop._dispatch 的 per-session asyncio.Lock 内调用，
  同一 session 不会并发执行压缩，无需 CompactManager 自建锁。
- 后台 idle 压缩（maybe_schedule_idle_compact）在 lock 外异步执行，
  通过 _compact_tasks 去重 + _compact_retry_after 冷却退避自行管理并发。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

from ftre_agent_core.llm import LLMError, LLMHandler, TextDelta

from ftre.bus import BusMessage

logger = logging.getLogger(__name__)

DEFAULT_PRECOMPACT_THRESHOLD = 0.5
DEFAULT_COMPACT_THRESHOLD = 0.7

# compress-fast 默认保留最近 N 条 tool_result 完整
DEFAULT_FAST_KEEP_RECENT = 3


@dataclass
class ContextCompactData:
    """context_compact 事件的 data 字段。

    两种模式：
      summary: 调 LLM 生成摘要，替换之前的消息
      fast:    零 LLM 成本，裁剪旧 tool_result 输出为占位符
    """

    # ── 公共字段 ──
    mode: Literal["summary", "fast"] = "summary"
    trigger: Literal["auto", "manual", "idle"] = "auto"
    silent: bool = True

    # ── fast 模式：被裁剪的 event id 列表 ──
    events: list[str] = field(default_factory=list)

    # ── summary 模式 ──
    summary: str = ""
    events_before: int = 0
    trigger_ratio: float = 0.0
    enable_ratio: float = 0.0
    tokens_before: int = 0
    tokens_after: int = 0

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
        bus,
        threshold: float = DEFAULT_COMPACT_THRESHOLD,
    ):
        self.session_manager = session_manager
        self.bus = bus
        self._threshold = threshold
        self._last_llm_errors: dict[str, LLMError | None] = {}

        # 后台 idle compact task 去重：session_id → asyncio.Task
        # 同一 session 同一时间只允许一个 compact task 在飞
        self._compact_tasks: dict[str, asyncio.Task] = {}
        self._compact_retry_after: dict[str, float] = {}

    # ─── 只读判断 ──────────────────────────────────────────────────

    async def should_compact(
        self, session_id: str, channel_id: str, config, *, threshold: float | None = None
    ) -> bool:
        """水位是否超过 threshold？只读 DB，不调 LLM。

        优先用 API 报告的真实 token（get_token_usage 的 anchor + pending 策略），
        全新 session 无 anchor 时退化为字符估算。
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
        silent: bool = True,
        trigger: Literal["auto", "manual", "idle"] = "auto",
    ) -> str | None:
        """异步执行压缩。写入 context_compact(mode='summary') 直接生效。"""
        return await self._do_compact(
            session_id, channel_id,
            config=config, silent=silent, trigger=trigger,
        )

    # ─── 快速压缩（零 LLM 成本） ───────────────────────────────────

    async def compress_fast(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        silent: bool = False,
        keep_recent: int = DEFAULT_FAST_KEEP_RECENT,
    ) -> bool:
        """快速压缩：不调 LLM，直接裁剪旧 tool_result 输出为占位符。

        策略：保留最近 keep_recent 条 tool_result 完整，其余的 event id
        记录在 context_compact(mode=fast).events 中。to_openai_messages
        遇到这些 id 的 tool_result 时替换为 "[工具输出已压缩]"。

        Returns:
            True: 执行了裁剪
            False: 没有 tool_result 可裁剪
        """
        events = await self.session_manager.get_messages_by_session(session_id)
        if not events:
            return False

        # 只从最后一个 summary compact 之后找 tool_result
        # 之前的事件已被 summary 替换，to_openai_messages 不会加载它们
        cursor_idx = get_cursor_index(events)
        active_events = events[cursor_idx:]

        # 找活跃区间内的 tool_result，排除已被之前 fast compact 标记的
        already_compacted: set[str] = set()
        for e in active_events:
            if e.get("type") == "context_compact" and (e.get("data") or {}).get("mode") == "fast":
                already_compacted.update((e.get("data") or {}).get("events", []))
        tool_results = [
            e for e in active_events
            if e.get("type") == "tool_result" and e.get("id") not in already_compacted
        ]
        if len(tool_results) <= keep_recent:
            logger.info(
                f"[compact-fast] session={session_id} tool_result 数 "
                f"{len(tool_results)} <= keep_recent={keep_recent}，跳过"
            )
            return False

        to_compact = tool_results[:-keep_recent] if keep_recent > 0 else tool_results
        compacted_ids = [e.get("id", "") for e in to_compact if e.get("id")]

        # 估算压缩前后 token（只算活跃区间）
        from ftre.session.token_counter import estimate_messages_tokens
        tokens_before = estimate_messages_tokens(active_events)

        payload = asdict(ContextCompactData(
            mode="fast",
            trigger="manual",
            silent=silent,
            events=compacted_ids,
        ))

        # tokens_after = tokens_before 减去被裁剪的 tool_result 估算差值
        compacted_events = to_compact
        compacted_tokens = estimate_messages_tokens(compacted_events)
        tokens_after = max(0, tokens_before - compacted_tokens)
        payload["tokens_before"] = tokens_before
        payload["tokens_after"] = tokens_after

        try:
            await self.session_manager.save_message(
                session_id, "context_compact", payload,
                timestamp=time.time(),
            )
        except Exception:
            logger.exception(f"[compact-fast] 写入 DB 失败 session={session_id}")
            return False

        # 通知前端
        done_data = {
            "mode": "fast",
            "events": len(compacted_ids),
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
        }
        if silent:
            done_data["silent"] = True
        await self._notify(session_id, channel_id, "context_compact_done", done_data, silent=silent)

        logger.info(
            f"[compact-fast] session={session_id} 裁剪 {len(compacted_ids)} 条 tool_result, "
            f"tokens {tokens_before} → {tokens_after}"
        )
        return True

    async def _do_compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        silent: bool,
        trigger: Literal["auto", "manual", "idle"] = "auto",
    ) -> str | None:
        """压缩主逻辑：读事件 → LLM 摘要 → 写 compact event。"""

        # 取消在飞的后台 idle 压缩，避免与本次前台压缩竞态
        existing = self._compact_tasks.get(session_id)
        if existing and not existing.done():
            existing.cancel()
            self._compact_tasks.pop(session_id, None)
            logger.info(f"[compact] session={session_id} 取消后台压缩，改用前台压缩")

        # 1. 读取事件流
        events = await self.session_manager.get_messages_by_session(session_id)
        if not events:
            logger.info(f"[compact] session={session_id} 无事件，跳过")
            await self._notify_failed(session_id, channel_id, "当前会话没有历史消息", silent=silent)
            return None

        # 2. 从上一个 summary compact 之后开始，全量压缩
        cursor_idx = get_cursor_index(events)
        head_events = events[cursor_idx:]
        if not head_events:
            logger.info(f"[compact] session={session_id} head_events 为空，跳过")
            return None

        # 3. 估算当前 token（优先用 API 真实值）
        cw = getattr(config.llm, "context_window", None)
        if not cw or cw <= 0:
            logger.info(f"[compact] session={session_id} context_window={cw} 无效，跳过")
            return None
        usage = await self.session_manager.get_token_usage(session_id)
        tokens_before = usage["total"]
        current_ratio = tokens_before / cw

        # 4. 通知前端开始
        await self._notify(session_id, channel_id, "context_compact_start", {
            "events": len(head_events),
            "tokens": tokens_before,
        }, silent=silent)

        # 5. 取之前摘要（如果有）
        previous_summary = get_previous_summary(events)

        # 6. LLM 直调摘要
        summary = await self._run_compact_llm(
            head_events, config=config, previous_summary=previous_summary,
            session_id=session_id,
        )
        if not summary:
            logger.warning(f"[compact] session={session_id} LLM 摘要失败")
            await self._notify_failed(session_id, channel_id, "LLM 摘要未产出合格结果", silent=silent)
            return None

        # 7. 写入 compact event（timestamp=当前时间，放末尾）
        now = time.time()
        payload: dict = asdict(ContextCompactData(
            mode="summary",
            trigger=trigger,
            silent=silent,
            summary=summary,
            events_before=len(head_events),
            trigger_ratio=current_ratio,
            enable_ratio=getattr(config.context, "compact_threshold", DEFAULT_COMPACT_THRESHOLD),
            tokens_before=tokens_before,
        ))
        synthetic = {
            "type": "context_compact",
            "data": payload,
            "timestamp": now,
        }
        from ftre.session.token_counter import estimate_messages_tokens
        tokens_after = estimate_messages_tokens([synthetic])

        # 膨胀保护：摘要比原文还大 → 放弃
        if tokens_after >= tokens_before:
            logger.warning(
                f"[compact] session={session_id} 摘要膨胀 {tokens_before} → {tokens_after}，放弃"
            )
            await self._notify_failed(
                session_id, channel_id, "压缩后体积未减小", silent=silent,
            )
            return None

        payload["tokens_after"] = tokens_after

        try:
            await self.session_manager.save_message(
                session_id, "context_compact", payload,
                timestamp=now,
            )
        except Exception:
            logger.exception(f"[compact] 写入 DB 失败 session={session_id}")
            return None

        # 8. 通知前端完成
        done_data: dict = {
            "mode": "summary",
            "events": len(head_events),
            "tokens_before": tokens_before,
            "tokens_after": payload.get("tokens_after"),
            "summary": _preview(summary),
        }
        if silent:
            done_data["silent"] = True
        await self._notify(session_id, channel_id, "context_compact_done", done_data, silent=silent)

        logger.info(
            f"[compact] session={session_id} 压缩完成 "
            f"events={len(head_events)}，摘要 {len(summary)} 字符"
        )
        return summary

    # ─── LLM 直调摘要 ──────────────────────────────────────────────

    async def _run_compact_llm(
        self,
        head_events: list[dict],
        *,
        config,
        previous_summary: str | None = None,
        session_id: str = "",
    ) -> str | None:
        """调用 LLM 生成摘要（异步）。

        采用 OpenCode 的 serialize → select → buildPrompt 模式：
        1. 序列化 head 事件为纯文本（截断 tool 输出至 2000 字符）
        2. buildPrompt() 返回多条 user message：对话记录 + 指令/模板
        3. 多条 user message 依次发给 LLM
        """
        self._last_llm_errors[session_id] = None
        try:
            context = _serialize_events(head_events)
            if not context.strip():
                logger.debug("[compact] 事件文本为空，跳过 LLM 调用")
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

    async def _notify(self, session_id: str, channel_id: str, event_type: str, data: dict,
                      *, silent: bool = False) -> None:
        """派发 outbound 事件通知前端。"""
        try:
            payload = dict(data)
            if silent:
                payload["silent"] = True
            msg = BusMessage(
                type="agent_event",
                from_channel=channel_id,
                to_channel=channel_id,
                from_session=session_id,
                to_session=session_id,
                data={"type": event_type, "data": payload},
            )
            await self.bus.publish_outbound(msg)
        except Exception:
            logger.debug(f"[compact] 通知前端失败: {event_type}")

    async def _notify_failed(self, session_id: str, channel_id: str, reason: str,
                             *, silent: bool = False) -> None:
        """派发 context_compact_failed 通知。"""
        await self._notify(session_id, channel_id, "context_compact_failed", {"reason": reason}, silent=silent)

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

            # 去重：同一 session 已有后台 compact 在飞则跳过
            if session_id in self._compact_tasks:
                logger.debug(f"[compact] session={session_id} 已有后台压缩在飞，跳过")
                return

            # 后台隐形压缩：直接启用 compact event，让下一轮上下文立刻使用摘要。
            async def _do_bg_compact():
                try:
                    summary = await self.compact(
                        session_id, channel_id,
                        config=config,
                        silent=getattr(config.context, "silent", True),
                        trigger="idle",
                    )
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
                finally:
                    self._compact_tasks.pop(session_id, None)

            task = asyncio.create_task(_do_bg_compact())
            self._compact_tasks[session_id] = task
            logger.info(f"[compact] idle 后台压缩已派发 session={session_id}")
        except Exception:
            logger.exception(f"[compact] idle 压缩调度异常 session={session_id}")

    def cancel_all_compact_tasks(self) -> None:
        """stop() 时调用，取消所有在飞的后台压缩 task。"""
        for task in self._compact_tasks.values():
            task.cancel()
        self._compact_tasks.clear()


# ─── 模块级纯函数（可单测） ───────────────────────────────────────────


def _serialize_events(
    chunk: list[dict],
    *,
    tool_output_max_chars: int = 2000,
) -> str:
    """把消息列表序列化为 LLM 可读的纯文本（源自 OpenCode serialize 模式）。

    规则（对齐 OpenCode）：
    - 用户消息：[User]: 内容
    - AI 回复：[Assistant]: 内容（从 content[] 的 text 块提取）
    - AI 思考：[Assistant reasoning]: 内容（从 content[] 的 thinking 块提取）
    - 工具调用：[Assistant tool call]: name(args)（从 content[] 的 toolCall 块提取）
    - 工具结果：[Tool result]: 输出（截断至 tool_output_max_chars）
    - 工具错误：[Tool error]: 错误信息
    - 多条消息之间用 \\n\\n 分隔
    """
    TOOL_OUTPUT_MAX_CHARS = tool_output_max_chars
    parts: list[str] = []

    for ev in chunk:
        t = ev.get("type", "")
        d = ev.get("data") or {}

        if t == "assistant_message_complete":
            blocks = d.get("content", [])
            for b in blocks:
                bt = b.get("type", "")
                if bt == "thinking" and b.get("thinking"):
                    parts.append(f"[Assistant reasoning]: {b['thinking']}")
                elif bt == "text" and b.get("text"):
                    parts.append(f"[Assistant]: {b['text']}")
                elif bt == "toolCall":
                    args_str = json.dumps(b.get("arguments", {}), ensure_ascii=False)
                    parts.append(f"[Assistant tool call]: {b.get('name', '?')}({args_str})")

        elif t == "tool_result":
            result = d.get("result", "")
            if len(result) > TOOL_OUTPUT_MAX_CHARS:
                result = result[:TOOL_OUTPUT_MAX_CHARS] + "\n[truncated]"
            if d.get("error"):
                parts.append(f"[Tool error]: {result}")
            else:
                parts.append(f"[Tool result]: {result}")

        elif t == "user_message":
            content = d.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(p.get("text") or p.get("data") or "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            attachments = d.get("attachments") or []
            att_lines = [
                f"[附件 {a.get('mime_type', a.get('mime', '未知'))}: {a.get('name', a.get('uri', ''))}]"
                for a in attachments if isinstance(a, dict)
            ]
            lines = [f"[User]: {content}"] + att_lines
            parts.append("\n".join(lines))

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
    body = re.sub(r'[，。！？；：""''（）【】《》…—\s,.!?;:\'\"()\[\]{}]', '', no_md)
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


def get_cursor_index(events: list[dict]) -> int:
    """返回最新 context_compact(summary) 事件之后的索引。无则返回 0。"""
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        if ev.get("type") != "context_compact":
            continue
        mode = (ev.get("data") or {}).get("mode", "summary")
        if mode == "summary":
            return i + 1
    return 0


def get_previous_summary(events: list[dict]) -> str | None:
    """返回最新 context_compact(summary) 事件的 summary。"""
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        if ev.get("type") != "context_compact":
            continue
        mode = (ev.get("data") or {}).get("mode", "summary")
        if mode == "summary":
            return (ev.get("data") or {}).get("summary") or None
    return None


def _preview(text: str, limit: int = 200) -> str:
    return text[:limit] + "..." if len(text) > limit else text
