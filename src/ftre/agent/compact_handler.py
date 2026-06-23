"""
CompactHandler — 上下文压缩处理器

设计：
- 50% 水位：idle 时隐形压缩 → compact(enabled=True)，直接写入已启用 compact event
- 60% 水位：关键路径 → enable_pending_compact() 启用历史 pending，没有则 compact(enabled=True)
- /compact 手动：先 enable_pending_compact()，没有则 compact(enabled=True, silent=False)

每次压缩：从上一个 enabled=True 的 compact 到现在，全量 LLM 摘要，
compact event 放末尾（timestamp=触发时间）。

to_openai_messages 遍历：
- enabled=False → continue（跳过，不参与上下文）
- enabled=True → 清空之前所有 messages + 注入 summary

并发安全：
- compact() 总是在 AgentLoop._dispatch 的 per-session asyncio.Lock 内调用，
  同一 session 不会并发执行压缩，无需 CompactHandler 自建锁。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from ftre_agent_core.agent.event import (
    AgentEvent,
    AssistantMessageCompleteEvent,
    ReasoningCompleteEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from ftre_agent_core.llm import LLMError, LLMHandler, TextDelta

from ftre.bus import BusMessage

logger = logging.getLogger(__name__)

DEFAULT_PRECOMPACT_THRESHOLD = 0.5
DEFAULT_COMPACT_THRESHOLD = 0.6

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
你是一个对话历史压缩助手。你需要把对话历史整理成一份"交接摘要"——目标是让一个完全没看过原始对话的 AI 读完后能无缝接着干活。

严格遵守上面给出的摘要模板结构，每个段落都要有内容。"""


class CompactHandler:
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
        self._last_llm_error: LLMError | None = None

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
        from ftre.session.token_counter import estimate_events_tokens
        estimated = estimate_events_tokens(events)
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
        from ftre.session.token_counter import estimate_events_tokens
        synthetic = {
            "type": "context_compact",
            "data": data,
            "timestamp": pending_event.get("timestamp", time.time()),
        }
        # 启用后，这条 compact 之后的 tail 事件 + compact 自身
        tail_events = events[pending_idx + 1:]
        data["tokens_after"] = estimate_events_tokens([synthetic, *tail_events])

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
        from ftre.session.token_counter import estimate_events_tokens
        tokens_before = estimate_events_tokens(events)
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
            payload["tokens_after"] = estimate_events_tokens([synthetic])
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
    ) -> str | None:
        """调用 LLM 生成摘要（异步）。

        采用 OpenCode 的 serialize → select → buildPrompt 模式：
        1. 序列化 head 事件为纯文本（截断 tool 输出至 2000 字符）
        2. buildPrompt() 拼接：[增量指令/首次指令] + SUMMARY_TEMPLATE + 序列化文本
        3. 把拼接结果作为 user message 发给 LLM
        """
        self._last_llm_error = None
        try:
            context = _serialize_events(head_events)
            if not context.strip():
                logger.debug("[compact] 事件文本为空，跳过 LLM 调用")
                return None

            prompt_text = _build_prompt(previous_summary=previous_summary, context=[context])

            messages = [
                {"role": "system", "content": COMPACT_LLM_SYSTEM_PROMPT},
                {"role": "user", "content": prompt_text},
            ]

            # 优先使用 compact_llm，未配置则回退到主 llm
            llm_cfg = getattr(config, "compact_llm", None) or config.llm
            handler = LLMHandler(
                model=llm_cfg.model,
                api_key=llm_cfg.api_key,
                api_base=llm_cfg.api_base,
                api_type=llm_cfg.api_type,
            )

            collected: list[str] = []
            async for ev in handler.stream(messages):
                if isinstance(ev, TextDelta):
                    collected.append(ev.text)

            summary = "".join(collected).strip()
            if not summary or len(summary) < 200 or "## " not in summary:
                logger.warning(f"[compact] LLM 摘要不合格 len={len(summary)}")
                return None
            return summary
        except LLMError as exc:
            self._last_llm_error = exc
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


# ─── 模块级纯函数（可单测） ───────────────────────────────────────────


def _serialize_events(
    chunk: list[dict],
    *,
    tool_output_max_chars: int = 2000,
) -> str:
    """把事件流序列化为 LLM 可读的纯文本（源自 OpenCode serialize 模式）。

    规则（对齐 OpenCode）：
    - 用户消息：[User]: 内容
    - AI 回复：[Assistant]: 内容
    - AI 思考：[Assistant reasoning]: 内容
    - 工具调用：[Assistant tool call]: name(args)
    - 工具结果：[Tool result]: 输出（截断至 tool_output_max_chars）
    - 工具错误：[Tool error]: 错误信息
    - 多条消息之间用 \\n\\n 分隔
    """
    TOOL_OUTPUT_MAX_CHARS = tool_output_max_chars
    parts: list[str] = []

    for ev in chunk:
        # 尝试转为 AgentEvent class；非 Agent 事件（context_compact）保留 dict
        agent_ev: AgentEvent | None = None
        try:
            agent_ev = AgentEvent.from_dict(ev) if ev.get("type", "") not in (
                "context_compact", "context_compact_enabled",
                "context_compact_failed",
            ) else None
        except (KeyError, ValueError):
            agent_ev = None

        if agent_ev is not None:
            if isinstance(agent_ev, AssistantMessageCompleteEvent):
                parts.append(f"[Assistant]: {agent_ev.content}")
            elif isinstance(agent_ev, ReasoningCompleteEvent):
                if agent_ev.content:
                    parts.append(f"[Assistant reasoning]: {agent_ev.content}")
            elif isinstance(agent_ev, ToolCallEvent):
                args_str = json.dumps(agent_ev.arguments, ensure_ascii=False)
                parts.append(f"[Assistant tool call]: {agent_ev.tool_name}({args_str})")
            elif isinstance(agent_ev, ToolResultEvent):
                result = agent_ev.result
                if len(result) > TOOL_OUTPUT_MAX_CHARS:
                    result = result[:TOOL_OUTPUT_MAX_CHARS] + "\n[truncated]"
                parts.append(f"[Tool result]: {result}")
            elif isinstance(agent_ev, UserMessageEvent):
                content = agent_ev.content
                if isinstance(content, list):
                    content = "\n".join(
                        str(p.get("text") or p.get("data") or "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                attachments = (ev.get("data") or {}).get("attachments") or []
                att_lines = [
                    f"[附件 {a.get('mime_type', a.get('mime', '未知'))}: {a.get('name', a.get('uri', ''))}]"
                    for a in attachments if isinstance(a, dict)
                ]
                lines = [f"[User]: {content}"] + att_lines
                parts.append("\n".join(lines))
            continue

        # ─── 非 Agent 事件仍用 dict 访问 ──────
        t = ev.get("type", "")
        d = ev.get("data") or {}

    return "\n\n".join(parts)


def _build_prompt(
    *,
    previous_summary: str | None = None,
    context: list[str] | None = None,
) -> str:
    """构建 LLM 摘要 prompt（源自 OpenCode buildPrompt 模式）。

    首次压缩：Create a new anchored summary from the conversation history.
    增量压缩：Update the anchored summary below using the conversation history above.
              Preserve still-true details, remove stale details, and merge in the new facts.

    拼接顺序：[指令 + previous-summary] + SUMMARY_TEMPLATE + context文本
    """
    if previous_summary:
        instruction = (
            "根据上方的对话历史，更新下方的锚定摘要。\n"
            "保留仍然正确的细节，移除过时的细节，合并新的事实。\n"
            f"<previous-summary>\n{previous_summary}\n</previous-summary>"
        )
    else:
        instruction = "根据对话历史，创建一份新的锚定摘要。"

    return "\n\n".join([instruction, SUMMARY_TEMPLATE, *(context or [])])


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
