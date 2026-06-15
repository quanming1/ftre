"""
CompactHandler — 上下文压缩处理器

设计：
- 50% 水位：idle 时预压缩 → compact(enabled=False)，写 pending compact event
- 60% 水位：关键路径 → enable_pending_compact() 启用 pending，没有则 compact(enabled=True)
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

from ftre_agent_core.llm import LLMHandler, TextDelta

from ftre.bus import BusMessage

logger = logging.getLogger(__name__)

DEFAULT_PRECOMPACT_THRESHOLD = 0.5
DEFAULT_COMPACT_THRESHOLD = 0.6

# LLM 直调摘要的 system prompt
COMPACT_LLM_SYSTEM_PROMPT = """\
你是一个对话历史压缩助手。下面 user 消息里附带一段对话事件流文本，你需要把它整理成
"交接文档"——目标是让一个完全没看过原始对话的 AI 读完后能无缝接着干活。

核心原则：
1. 用户原话（USER_INPUT/用户）——一字不漏照搬
2. AI 关键决策（选了什么方案、放弃了什么、原因）——原样保留
3. 关键技术事实（文件路径、函数签名、配置值、报错原文、关键代码片段）——原样引用
4. 探索性动作 / 反复试错 / 啰嗦解释——可压缩成一句结论
5. 拿不准时一律保留

输出格式（必须严格遵守，前端识别这个结构）：

## 轮次摘要

### 第 N 轮
**用户原话：** （完整保留）
**AI 关键决策：** （原样保留决策内容）
**做了什么：** （重要步骤照实写，纯流水账才概括）
**产出：** （改了什么 / 得出什么结论 / 卡在哪）
**关键技术事实：** （函数签名、代码片段、报错、配置、路径——原样保留）

## 交接状态

**已落地的成果：** （带完整路径与改动要点）
**关键技术事实：** （后续必须知道的硬信息）
**重要代码/片段：** （核心部分原样保留）
**已确定的决策：** （选定的方案、否决的方案及原因）
**待办与悬而未决：** （下一步做什么、遗留 bug、等待确认的点）

直接输出摘要本身，不要任何前言、解释或元评论。
"""


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
            enabled: False → 预压缩（50% 水位），写 pending compact
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
        """调用 LLM 生成摘要（异步）。"""
        try:
            text = _format_events_for_llm(head_events, previous_summary=previous_summary)
            if not text.strip():
                logger.debug("[compact] 事件文本为空，跳过 LLM 调用")
                return None

            messages = [
                {"role": "system", "content": COMPACT_LLM_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ]

            handler = LLMHandler(
                model=config.llm.model,
                api_key=config.llm.api_key,
                api_base=config.llm.api_base,
                api_type=config.llm.api_type,
            )

            collected: list[str] = []
            async for ev in handler.stream(messages):
                if isinstance(ev, TextDelta):
                    collected.append(ev.text)

            summary = "".join(collected).strip()
            if not summary or len(summary) < 300 or "## " not in summary:
                logger.warning(f"[compact] LLM 摘要不合格 len={len(summary)}")
                return None
            return summary
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


def _format_events_for_llm(
    chunk: list[dict],
    *,
    previous_summary: str | None = None,
    max_chars: int = 60000,
) -> str:
    """把事件流格式化为 LLM 可读的 markdown 文本。

    max_chars: 输出文本总长度上限。超过时对中间轮次只保留标题行
    （用户原话），详细内容只保留前 3 轮和最近 5 轮。
    """
    if not chunk and not previous_summary:
        return ""

    # 先正常格式化，然后如果超过 max_chars，对中间轮次裁剪
    # 需要按轮次分组
    turn_idx = 0
    turns: list[tuple[int, list[str]]] = []  # (turn_number, lines)
    current_turn_lines: list[str] = []

    for ev in chunk:
        t = ev.get("type", "")
        d = ev.get("data") or {}
        if t == "USER_INPUT":
            # 上一个轮次结束
            if current_turn_lines:
                turns.append((turn_idx, current_turn_lines))
                current_turn_lines = []
            turn_idx += 1
            content = d.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(p.get("data", "") or "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            current_turn_lines.append(f"### 第 {turn_idx} 轮")
            current_turn_lines.append(f"**用户**：{content}")
        elif t == "message_complete":
            current_turn_lines.append(f"**AI**：{d.get('content', '')}")
        elif t == "reasoning_complete":
            content = d.get("content", "")
            if content:
                current_turn_lines.append(f"**思考**：{content}")
        elif t == "tool_call":
            name = d.get("name", "")
            args = d.get("arguments", {})
            args_str = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
            if len(args_str) > 300:
                args_str = args_str[:300] + "...[截断]"
            current_turn_lines.append(f"**工具调用**：{name}({args_str})")
        elif t == "tool_result":
            result = d.get("result", "")
            if not isinstance(result, str):
                result = str(result)
            if len(result) > 500:
                result = result[:500] + f"...[截断 {len(result) - 500} 字符]"
            current_turn_lines.append(f"**工具结果**：{result}")
    # 最后一轮
    if current_turn_lines:
        turns.append((turn_idx, current_turn_lines))

    # 组装输出
    lines: list[str] = ["## 对话事件流", ""]
    if previous_summary:
        lines.append("### 之前的历史摘要（沿用）")
        lines.append(previous_summary)
        lines.append("")

    total_len = len("\n".join(lines))
    keep_head = 3  # 保留前 3 轮完整内容
    keep_tail = 5  # 保留最近 5 轮完整内容
    total_turns = len(turns)

    for i, (num, turn_lines) in enumerate(turns):
        keep_full = (i < keep_head) or (i >= total_turns - keep_tail)
        turn_text = "\n".join(turn_lines)

        if keep_full:
            lines.append(turn_text)
        else:
            # 中间轮次：只保留标题行（第一行 = "### 第 N 轮"）和用户原话行
            title = turn_lines[0] if turn_lines else ""
            user_line = turn_lines[1] if len(turn_lines) > 1 else ""
            lines.append(title)
            lines.append(user_line)
            lines.append("**AI**：[中间轮次已省略，仅保留用户原话]")
            lines.append("")
            turn_text = "\n".join([title, user_line, "**AI**：[中间轮次已省略]", ""])

        total_len += len(turn_text) + 1
        if total_len > max_chars and i >= keep_head:
            # 即使是 keep_tail 的轮次也截断
            break

    # 如果还有没写完的轮次（keep_tail），追加它们
    if total_turns > keep_head + keep_tail:
        remaining_tail = turns[total_turns - keep_tail:]
        for num, turn_lines in remaining_tail:
            lines.append("\n".join(turn_lines))

    result = "\n".join(lines).strip()
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n[整体截断：文本过长，只保留前半部分]"

    return result


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
