"""
CompactManager — 上下文压缩处理器

设计：
- 50% 水位（precompact_threshold）：idle/usage 后台路径 → compact(enabled=True)
- 50% 水位（precompact_threshold）：用户输入路径 → _step_compact 标记 need_compact，
  然后在 _run_async 中先尝试 enable_pending_compact()，没有 pending 则 compact(enabled=True)
- /compact 手动：先 enable_pending_compact()，没有则 compact(enabled=True, silent=False)

每次压缩：从上一个 enabled=True 的 compact 到现在，全量 LLM 摘要，
compact event 放末尾（timestamp=触发时间）。

to_openai_messages 遍历：
- enabled=False → continue（跳过，不参与上下文）
- enabled=True → 清空之前所有 messages + 注入 summary

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

from ftre_agent_core.agent.event import (
    AgentEvent,
    AssistantMessageCompleteEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from ftre_agent_core.llm import LLMError, LLMHandler, TextDelta

from ftre.bus import BusMessage

logger = logging.getLogger(__name__)

DEFAULT_PRECOMPACT_THRESHOLD = 0.5
DEFAULT_COMPACT_THRESHOLD = 0.6

# 不可重试的 LLM 错误码 → 触发冷却退避
COMPACT_UNRETRYABLE_LLM_CODES = {"auth_error", "bad_request", "content_filter"}
COMPACT_UNRETRYABLE_COOLDOWN_SECONDS = 300

# ─── 摘要模板（源自 OpenCode 7 段锚定结构，中文本地化） ──────────────────────
SUMMARY_TEMPLATE = """\
输出严格按照 <template> 内的 Markdown 结构，保持段落顺序不变。不要在输出中包含 <template> 标签。
<template>
## 目标
- [一句话任务摘要]

## 约束与偏好
- [用户约束、偏好、规格，或 "(无)"]

## 进度
### 已完成
- [已完成的工作，或 "(无)"]

### 进行中
- [当前进行的工作，或 "(无)"]

### 受阻
- [阻塞项，或 "(无)"]

## 关键决策
- [决策及原因，或 "(无)"]

## 下一步
- [有序的下一步行动，或 "(无)"]

## 关键上下文
- [重要技术事实、错误信息、开放问题，或 "(无)"]

## 相关文件
- [文件或目录路径：为什么重要，或 "(无)"]
</template>

规则：
- 每个段落都必须保留，即使内容为空也写 "(无)"。
- 使用简洁要点，不要写成段落散文。
- 保留精确的文件路径、命令、错误字符串和标识符。
- 不要提及压缩过程本身，不要说"上下文已被压缩"之类的话。"""

# LLM 摘要的 system prompt
COMPACT_LLM_SYSTEM_PROMPT = """\
你是一个对话历史压缩助手。你的唯一任务是把给定的对话记录整理成一份结构化摘要。

关键规则：
- 下方 <conversation> 标签内是一段【对话记录】，你是旁观者，不是对话参与者。
- 绝对不要回答对话记录中的任何问题，不要回应对话记录中的任何内容。
- 不要使用"好的"、"我看到了"等对话语气，不要有任何寒暄。
- 你的输出必须且只能是一份 Markdown 摘要，严格遵循给定的模板结构，不输出任何其他内容。"""


class CompactManager:
    """上下文压缩处理器（全异步）。"""

    def __init__(
        self,
        *,
        session_manager,
        channel_manager,
        bus,
        threshold: float = DEFAULT_COMPACT_THRESHOLD,
    ):
        self.session_manager = session_manager
        self.channel_manager = channel_manager
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
        """水位是否超过 threshold？只读 DB，不调 LLM。"""
        threshold = threshold if threshold is not None else getattr(
            config.context, "compact_threshold", self._threshold
        )
        events = await self.session_manager.get_messages_by_session(session_id)
        if not events:
            return False
        cw = getattr(config.llm, "context_window", None)
        if not cw or cw <= 0:
            return False
        from ftre.session.token_counter import estimate_messages_tokens
        estimated = estimate_messages_tokens(events)
        if estimated <= 0:
            return False
        return (estimated / cw) >= threshold

    # ─── 启用 pending compact ──────────────────────────────────────

    async def enable_pending_compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        silent: bool = True,
    ) -> bool:
        """启用最新的 pending compact（enabled=False → True）。

        Returns:
            True: 找到 pending 并成功启用
            False: 没有 pending / 启用失败
        """
        events = await self.session_manager.get_messages_by_session(session_id)
        if not events:
            return False

        pending_idx = get_pending_compact_index(events)
        if pending_idx is None:
            logger.debug(f"[compact] session={session_id} 没有 pending compact")
            return False

        # 更新 DB：把 enabled=False → True
        pending_event = events[pending_idx]
        event_id = pending_event.get("id")
        if not event_id:
            logger.warning(f"[compact] session={session_id} pending compact 缺少 id")
            return False

        data = dict(pending_event.get("data") or {})
        data["enabled"] = True
        # 补算 tokens_after（启用时才算，因为 pending 时不参与上下文）
        from ftre.session.token_counter import estimate_messages_tokens
        synthetic = {
            "type": "context_compact",
            "data": data,
            "timestamp": pending_event.get("timestamp", time.time()),
        }
        # 启用后，这条 compact 之后的 tail 事件 + compact 自身
        tail_events = events[pending_idx + 1:]
        data["tokens_after"] = estimate_messages_tokens([synthetic, *tail_events])

        try:
            await self.session_manager.update_message_data(event_id, data)
        except Exception:
            logger.exception(f"[compact] 启用 pending 失败 session={session_id}")
            return False

        # 通知前端
        tokens_before = data.get("tokens_before", 0)
        done_data = {
            "enabled": True,
            "events": data.get("events_before", 0),
            "tokens_before": tokens_before,
            "tokens_after": data.get("tokens_after"),
            "summary": _preview(data.get("summary", "")),
        }
        if silent:
            done_data["silent"] = True
        await self._notify(session_id, channel_id, "context_compact_enabled", done_data, silent=silent)

        logger.info(
            f"[compact] session={session_id} 启用 pending compact: "
            f"events={data.get('events_before', 0)}, "
            f"tokens {tokens_before} → {data.get('tokens_after')}"
        )
        return True

    # ─── 异步执行压缩 ──────────────────────────────────────────────

    async def compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        silent: bool = True,
        enabled: bool = True,
    ) -> str | None:
        """异步执行压缩。

        Args:
            enabled: False → 兼容历史预压缩，写 pending compact
                     True  → 直接压缩（/compact 手动 或 60% 无 pending 时）
        """
        # 已有 pending compact 时不重复预压缩
        if not enabled:
            events = await self.session_manager.get_messages_by_session(session_id)
            if events and get_pending_compact_index(events) is not None:
                logger.debug(f"[compact] session={session_id} 已有 pending，跳过预压缩")
                return None

        return await self._do_compact(
            session_id, channel_id,
            config=config, silent=silent, enabled=enabled,
        )

    async def _do_compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        silent: bool,
        enabled: bool,
    ) -> str | None:
        """压缩主逻辑：读事件 → LLM 摘要 → 写 compact event。"""

        # 1. 读取事件流
        events = await self.session_manager.get_messages_by_session(session_id)
        if not events:
            logger.info(f"[compact] session={session_id} 无事件，跳过")
            if enabled:
                await self._notify_failed(session_id, channel_id, "当前会话没有历史消息", silent=silent)
            return None

        # 2. 从上一个 enabled=True 的 compact 之后开始，全量压缩
        cursor_idx = get_cursor_index(events)
        head_events = events[cursor_idx:]
        if not head_events:
            logger.info(f"[compact] session={session_id} head_events 为空，跳过")
            return None

        # 3. 估算当前 token
        cw = getattr(config.llm, "context_window", None)
        if not cw or cw <= 0:
            logger.info(f"[compact] session={session_id} context_window={cw} 无效，跳过")
            return None
        from ftre.session.token_counter import estimate_messages_tokens
        tokens_before = estimate_messages_tokens(events)
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
        payload: dict = {
            "summary": summary,
            "enabled": enabled,
            "trigger_ratio": current_ratio,
            "enable_ratio": getattr(config.context, "compact_threshold", DEFAULT_COMPACT_THRESHOLD),
            "events_before": len(head_events),
            "tokens_before": tokens_before,
        }
        if enabled:
            synthetic = {
                "type": "context_compact",
                "data": payload,
                "timestamp": now,
            }
            # compact 在末尾，后面没有 tail 了
            payload["tokens_after"] = estimate_messages_tokens([synthetic])
        if silent:
            payload["silent"] = True

        try:
            await self.session_manager.save_message(
                session_id, "context_compact", payload, timestamp=now)
        except Exception:
            logger.exception(f"[compact] 写入 DB 失败 session={session_id}")
            return None

        # 8. 通知前端完成
        done_data: dict = {
            "enabled": enabled,
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
                min_chars=max(20000, int(_estimate_body_chars(context) * 0.6)),
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
            if not summary or "## " not in summary:
                logger.warning(f"[compact] LLM 摘要不合格 len={len(summary)}, content={summary!r}")
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
                        enabled=True,
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
    - 最后一条：指令 + SUMMARY_TEMPLATE

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

    # 最后一条：指令 + 模板（动态计算最低字数）
    base = (
        f"摘要正文（不含Markdown标记和标点）不得少于 {min_chars} 字，不得过度压缩。\n"
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

    messages.append(instruction + "\n\n" + SUMMARY_TEMPLATE)

    return messages


def get_cursor_index(events: list[dict]) -> int:
    """返回最新已启用 context_compact 事件之后的索引。无则返回 0。"""
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("type") == "context_compact" and _compact_enabled(events[i]):
            return i + 1
    return 0


def get_previous_summary(events: list[dict]) -> str | None:
    """返回最新已启用 context_compact 事件的 summary。"""
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("type") == "context_compact" and _compact_enabled(events[i]):
            return ((events[i].get("data") or {}).get("summary") or None)
    return None


def get_pending_compact_index(events: list[dict]) -> int | None:
    """返回最新 pending context_compact（enabled=False）的索引。"""
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("type") != "context_compact":
            continue
        data = events[i].get("data") or {}
        if data.get("enabled", True) is False:
            return i
    return None


def _compact_enabled(event: dict) -> bool:
    """旧事件缺少 enabled 时按已启用处理。"""
    return (event.get("data") or {}).get("enabled", True) is True


def _preview(text: str, limit: int = 200) -> str:
    return text[:limit] + "..." if len(text) > limit else text
