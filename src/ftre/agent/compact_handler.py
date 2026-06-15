"""
CompactHandler — 上下文压缩处理器

职责：
- 判断会话是否到达压缩水位（token 占比超阈值）。
- LLM 直调摘要：把事件流格式化为文本，一次 chat completion 生成结构化摘要。
- 摘要写入 context_compact 事件到 DB；SessionManager.to_openai_messages 遇到该
  事件时丢弃之前所有 messages，用 summary 作为新起点。

入口：
- should_compact():  async 判断是否需要压缩。在 AgentLoop 压缩阶段里 await，
                      只读 DB，不调 LLM。
- compact():         同步执行压缩。通过 loop.run_in_executor 在线程里调用，
                      内部用 run_coroutine_threadsafe 把各步骤调度回主事件循环。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import weakref
from typing import Callable

from ftre_agent_core.llm import LLMHandler, StepFinish, TextDelta

from ftre.bus import BusMessage

logger = logging.getLogger(__name__)

DEFAULT_PRECOMPACT_THRESHOLD = 0.5
DEFAULT_COMPACT_THRESHOLD = 0.6
# 预算与目标：
# - budget = context_window - max_output - SAFETY_BUFFER
# - target = budget * CONSOLIDATION_RATIO
# 一次 compact 选边界把 prompt 估算降到 target 以下；
# 多轮压缩摊到多次空闲窗口，由游标只进不退保证不重做。
DEFAULT_CONSOLIDATION_RATIO = 0.5
DEFAULT_SAFETY_BUFFER = 1024
# context_window 配置缺省时 max_output 的兜底比例
_FALLBACK_MAX_OUTPUT_RATIO = 0.2

# 游标 timestamp 偏移：写 context_compact 时 ts = boundary.timestamp - EPSILON，
# 使其按 ASC 排序排在边界事件之前、tail 起点之上。EPSILON 必须 > sqlite REAL
# 精度的最小可分辨增量；这里取 1ms 量级足够安全（事件实际间隔通常 ≥ 数十 ms）。
CURSOR_TIMESTAMP_EPSILON = 0.001

# LLM 直调摘要的 system prompt：不让它写脚本读文件、不让它跑工具，
# 直接看内联文本输出 markdown。秒级返回。
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
    """上下文压缩处理器。"""

    def __init__(
        self,
        *,
        session_manager,
        channel_manager,
        bus,
        loop_getter: Callable[[], asyncio.AbstractEventLoop],
        threshold: float = DEFAULT_COMPACT_THRESHOLD,
        consolidation_ratio: float = DEFAULT_CONSOLIDATION_RATIO,
        safety_buffer: int = DEFAULT_SAFETY_BUFFER,
    ):
        self.session_manager = session_manager
        self.channel_manager = channel_manager
        self.bus = bus
        self._loop_getter = loop_getter
        self._threshold = threshold
        self._consolidation_ratio = consolidation_ratio
        self._safety_buffer = safety_buffer
        # 每 session 一把压缩锁，防止 idle 压缩与手动 /compact、
        # 或同 session 连续 idle 压缩撞车导致游标错乱。
        # WeakValueDictionary 让无人持有的锁自动回收。
        self._locks: weakref.WeakValueDictionary[str, threading.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._locks_guard = threading.Lock()

    def _get_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    # ─── 只读判断 ──────────────────────────────────────────────────

    async def should_compact(
        self, session_id: str, channel_id: str, config, *, threshold: float | None = None
    ) -> bool:
        """水位是否超过 threshold？只读 DB，不调 LLM，可在消费循环里安全 await。"""
        threshold = threshold if threshold is not None else getattr(
            config.context, "compact_threshold", self._threshold
        )
        ratio = await self._estimate_ratio(session_id, channel_id, config)
        if ratio is None:
            return False
        return ratio >= threshold

    async def _estimate_ratio(
        self, session_id: str, channel_id: str, config
    ) -> float | None:
        """估算当前 prompt token 占 context_window 的比例。"""
        events = await self.session_manager.get_messages_by_session(session_id)
        if not events:
            return None
        cw = getattr(config.llm, "context_window", None)
        if not cw or cw <= 0:
            return None
        from ftre.session.token_counter import estimate_events_tokens
        estimated = estimate_events_tokens(events)
        if estimated <= 0:
            return None
        return estimated / cw

    def _ratio_from_events(self, events: list[dict], config) -> float | None:
        """从已读取事件估算水位。"""
        cw = getattr(config.llm, "context_window", None)
        if not cw or cw <= 0:
            return None
        from ftre.session.token_counter import estimate_events_tokens
        estimated = estimate_events_tokens(events)
        if estimated <= 0:
            return None
        return estimated / cw

    # ─── 同步执行压缩 ──────────────────────────────────────────────

    def compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        silent: bool = True,
        enabled: bool = True,
        mode: str = "force",
    ) -> str | None:
        """同步执行压缩（预期在线程里调用）。

        Args:
            session_id: 会话 ID
            channel_id: 通道 ID
            config: AgentConfig，读 context 字段
            silent: True → 前端不渲染气泡（后台自动压缩）；False → 渲染气泡（手动 /compact）

        Returns:
            摘要文本，或 None（不需要压缩 / 压缩失败）
        """
        lock = self._get_lock(session_id)
        if not lock.acquire(blocking=False):
            logger.debug(f"[compact] session={session_id} 锁冲突，跳过")
            return None
        try:
            return self._do_compact(
                session_id,
                channel_id,
                config=config,
                silent=silent,
                enabled=enabled,
                mode=mode,
            )
        finally:
            lock.release()

    def enable_pending_compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        silent: bool = True,
    ) -> bool:
        """启用最新 pending context_compact。返回是否成功启用。"""
        lock = self._get_lock(session_id)
        if not lock.acquire(blocking=False):
            logger.debug(f"[compact] session={session_id} 启用锁冲突，跳过")
            return False
        try:
            events = self._await(
                self.session_manager.get_messages_by_session(session_id)
            )
            idx = get_pending_compact_index(events)
            if idx is None:
                return False
            event = events[idx]
            data = dict(event.get("data") or {})
            data["enabled"] = True
            ratio = self._ratio_from_events(events, config)
            if ratio is not None:
                data["enabled_at_ratio"] = ratio
            data["enabled_at"] = time.time()

            from ftre.session.token_counter import estimate_events_tokens
            tokens_before = estimate_events_tokens(events)
            enabled_events = [dict(ev) for ev in events]
            enabled_events[idx] = {**enabled_events[idx], "data": data}
            tokens_after = estimate_events_tokens(enabled_events)
            data["tokens_after"] = tokens_after

            self._await(self.session_manager.update_message_data(event["id"], data))
            done_data = {
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "summary": _preview(data.get("summary", "")),
            }
            if silent:
                done_data["silent"] = True
            self._notify(
                session_id,
                channel_id,
                "context_compact_enabled",
                done_data,
                silent=silent,
            )
            logger.info(
                f"[compact] session={session_id} 启用 pending 摘要 tokens {tokens_before}->{tokens_after}"
            )
            return True
        except Exception:
            logger.exception(f"[compact] 启用 pending 失败 session={session_id}")
            return False
        finally:
            lock.release()

    def _do_compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        silent: bool,
        enabled: bool,
        mode: str,
    ) -> str | None:
        """压缩主逻辑：读事件 → 算边界 → LLM 摘要 → 写游标事件。"""
        # 1. 读取事件流
        events = self._await(
            self.session_manager.get_messages_by_session(session_id)
        )
        if not events:
            return None
        if not enabled and get_pending_compact_index(events) is not None:
            logger.debug(f"[compact] session={session_id} 已有 pending compact，跳过准备")
            return None

        # 2. 算预算、目标
        cw = getattr(config.llm, "context_window", None)
        if not cw or cw <= 0:
            return None
        mo = getattr(config.llm, "max_output", None)
        ratio = getattr(config.context, "consolidation_ratio", self._consolidation_ratio)
        sb = getattr(config.context, "safety_buffer", self._safety_buffer)
        budget, target = calculate_budget_target(
            cw, mo, safety_buffer=sb, consolidation_ratio=ratio,
        )
        if budget <= 0 or target <= 0:
            return None

        # 3. 估算当前 token
        from ftre.session.token_counter import estimate_events_tokens
        estimated = estimate_events_tokens(events)
        if estimated <= 0:
            return None
        current_ratio = estimated / cw

        tokens_to_remove = estimated - target
        if tokens_to_remove <= 0:
            logger.debug(f"[compact] session={session_id} 无需压缩 estimated={estimated} target={target}")
            return None

        # 4. 选边界
        cursor_idx = get_cursor_index(events)
        boundary_idx = pick_compaction_boundary(events, cursor_idx, tokens_to_remove)
        if boundary_idx is None:
            logger.debug(f"[compact] session={session_id} 找不到合法边界")
            return None

        # 5. 切分 head / tail
        head_events = events[cursor_idx:boundary_idx]
        tail_events = events[boundary_idx:]
        if not head_events:
            return None

        # 6. 通知前端开始
        from ftre.session.token_counter import estimate_events_tokens as _eet
        tokens_before = _eet(events)
        self._notify(session_id, channel_id, "context_compact_start", {
            "events": len(head_events),
            "tokens": tokens_before,
            "mode": mode,
        }, silent=silent)

        # 7. LLM 直调摘要
        previous_summary = get_previous_summary(events)
        summary = self._run_compact_llm(
            head_events, config=config, previous_summary=previous_summary,
        )
        if not summary:
            logger.warning(f"[compact] session={session_id} LLM 摘要失败")
            self._notify(session_id, channel_id, "context_compact_failed", {
                "reason": "LLM 摘要未产出合格结果",
            }, silent=silent)
            return None

        # 8. 写入游标事件
        boundary_ts = None
        if boundary_idx < len(events):
            boundary_ts = events[boundary_idx].get("timestamp")
        cursor_ts = None
        if boundary_ts is not None:
            cursor_ts = float(boundary_ts) - CURSOR_TIMESTAMP_EPSILON
        payload = {
            "summary": summary,
            "enabled": enabled,
            "trigger_ratio": current_ratio,
            "enable_ratio": getattr(config.context, "compact_threshold", DEFAULT_COMPACT_THRESHOLD),
            "events_before": len(head_events),
            "tail_turns": _count_user_turns(tail_events),
        }
        if tokens_before is not None:
            payload["tokens_before"] = tokens_before
        if enabled:
            synthetic = {
                "type": "context_compact",
                "data": payload,
                "timestamp": cursor_ts if cursor_ts is not None else time.time(),
            }
            payload["tokens_after"] = estimate_events_tokens([synthetic, *tail_events])
        if silent:
            payload["silent"] = True

        try:
            self._await(self.session_manager.save_message(
                session_id, "context_compact", payload, timestamp=cursor_ts))
        except Exception:
            logger.exception(f"[compact] 写入 DB 失败 session={session_id}")
            return None

        done_data = {
            "enabled": enabled,
            "events": len(head_events),
            "tokens_before": tokens_before,
            "tokens_after": payload.get("tokens_after"),
            "summary": _preview(summary),
        }
        if silent:
            done_data["silent"] = True
        self._notify(session_id, channel_id, "context_compact_done", done_data, silent=silent)

        logger.info(
            f"[compact] session={session_id} 压缩完成 "
            f"tail_turns={_count_user_turns(tail_events)}，摘要 {len(summary)} 字符"
        )
        return summary

    # ─── LLM 直调摘要 ──────────────────────────────────────────────

    def _run_compact_llm(
        self,
        head_events: list,
        *,
        config,
        previous_summary: str | None = None,
    ) -> str | None:
        """直调 LLM 摘要（无工具，秒级）。

        把事件流格式化为文本 → 内联进 LLM prompt → 一次 chat completion → 出摘要。
        """
        # 1. 格式化事件流为文本
        body = _format_events_for_llm(head_events, previous_summary=previous_summary)
        if not body:
            return None

        # 2. 字符预算：避免超长文本撑爆 prompt
        cw = getattr(config.llm, "context_window", None) or 128000
        mo = getattr(config.llm, "max_output", None) or int(cw * _FALLBACK_MAX_OUTPUT_RATIO)
        char_budget = int((cw - mo - DEFAULT_SAFETY_BUFFER) * 4)  # 粗估 4 字符/token
        char_budget = max(char_budget, 20000)  # 下限
        if len(body) > char_budget:
            head_len = char_budget // 2
            tail_len = char_budget - head_len - 50
            body = body[:head_len] + "\n\n…[L1 修剪]…\n\n" + body[-tail_len:]

        # 3. 构建 LLM 请求
        messages = [
            {"role": "system", "content": COMPACT_LLM_SYSTEM_PROMPT},
            {"role": "user", "content": body},
        ]
        if previous_summary:
            messages[-1]["content"] = (
                f"以下是之前的摘要，请在此基础上更新而非重建：\n\n"
                f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
                f"---\n\n以下是新的对话事件流：\n\n{body}"
            )

        # 4. 一次 chat completion（通过 _await 调度到主事件循环，避免嵌套 asyncio.run）
        try:
            handler = LLMHandler(
                model=config.llm.model,
                api_key=config.llm.api_key,
                api_base=config.llm.api_base,
                api_type=config.llm.api_type,
            )
            collected: list[str] = []
            async def _collect():
                async for ev in handler.stream(messages=messages, tools=None):
                    if isinstance(ev, TextDelta):
                        collected.append(ev.text)
                    elif isinstance(ev, StepFinish):
                        pass  # 流结束信号，不需要处理
            self._await(_collect(), timeout=120)

            summary = "".join(collected).strip()
            if not summary or len(summary) < 300 or "## " not in summary:
                logger.warning(f"[compact] LLM 摘要不合格 len={len(summary)}")
                return None
            return summary
        except Exception:
            logger.exception("[compact] LLM 直调摘要异常")
            return None

    # ─── 工具方法 ──────────────────────────────────────────────────

    def _await(self, coro, timeout: int = 30):
        """在线程里等待一个协程完成。"""
        loop = self._loop_getter()
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    def _notify(self, session_id: str, channel_id: str, event_type: str, data: dict,
                *, silent: bool = False) -> None:
        """派发 outbound 事件通知前端。

        silent=True 时在事件 data 里打上 silent 标记，前端据此不渲染气泡
        （后台空闲压缩用，实现"无感"）。
        """
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
            self._await(self.bus.publish_outbound(msg), timeout=5)
        except Exception:
            logger.debug(f"[compact] 通知前端失败: {event_type}")


# ─── 模块级算法工具（纯函数，可单测，不依赖 CompactHandler 实例） ───────────


def calculate_budget_target(
    context_window: int,
    max_output: int | None,
    *,
    safety_buffer: int = DEFAULT_SAFETY_BUFFER,
    consolidation_ratio: float = DEFAULT_CONSOLIDATION_RATIO,
) -> tuple[int, int]:
    """计算可用输入预算与压缩目标。

    budget = context_window - max_output - safety_buffer
    target = budget * consolidation_ratio

    - max_output 为 None / 0 / 负数时退回 context_window * 0.2 兜底。
    - context_window <= 0 时返回 (0, 0)（调用方据此放弃压缩）。
    """
    if context_window <= 0:
        return 0, 0
    mo = int(max_output) if max_output and max_output > 0 else int(context_window * _FALLBACK_MAX_OUTPUT_RATIO)
    budget = max(0, context_window - mo - safety_buffer)
    target = int(budget * consolidation_ratio)
    return budget, target


def pick_compaction_boundary(
    events: list[dict],
    cursor_idx: int,
    tokens_to_remove: int,
) -> int | None:
    """在 events[cursor_idx:] 范围内选一个 user-turn 起点作为压缩边界。

    Args:
        events: 完整事件流（按 timestamp 升序，由 SessionManager 给出）。
        cursor_idx: 上次游标（最新 context_compact 的位置 + 1）；首次压缩为 0。
        tokens_to_remove: 期望本次压缩移除的 token 数。

    Returns:
        边界事件在 events 中的索引（该边界之前是 head/被摘要、之后是 tail/保留）。
        - 累计移除 token ≥ tokens_to_remove 时返回首个达标的 user-turn 起点。
        - 扫到尾仍不够：返回最后一个合法 user-turn 起点（能压多少压多少），
          但前提是它后面至少还有一个 user-turn（保证 tail 非空、不退化为全切）。
        - 找不到任何合法边界：返回 None。

    保证：返回索引 i 满足 events[i]["type"] == "USER_INPUT"，从而：
        - assistant 的 tool_calls 不会与 tool 的 tool_result 被切开（tool 配对不变量）。
    """
    from ftre.session.token_counter import _estimate_message
    from ftre.session.manager import SessionManager

    if cursor_idx >= len(events) or tokens_to_remove <= 0:
        return None

    # 收集 cursor_idx 之后所有 USER_INPUT 索引（user-turn 起点）
    user_turn_indices: list[int] = []
    for i in range(cursor_idx, len(events)):
        if events[i].get("type") == "USER_INPUT":
            user_turn_indices.append(i)

    if len(user_turn_indices) < 2:
        return None

    # 最末 USER_INPUT 永远不能作为边界——切在那里 tail 为空 = 全切。
    last_user_idx = user_turn_indices[-1]
    candidates = [idx for idx in user_turn_indices if idx != last_user_idx]
    if not candidates:
        return None

    removed = 0
    last_safe: int | None = None
    cur = cursor_idx
    for boundary_idx in candidates:
        if boundary_idx == cursor_idx:
            continue
        chunk = events[cur:boundary_idx]
        chunk_msgs = SessionManager.to_openai_messages(chunk)
        chunk_tokens = sum(_estimate_message(m) for m in chunk_msgs)
        removed += chunk_tokens
        last_safe = boundary_idx
        if removed >= tokens_to_remove:
            return boundary_idx
        cur = boundary_idx

    return last_safe


def _format_events_for_llm(
    chunk: list[dict],
    *,
    previous_summary: str | None = None,
) -> str:
    """把事件流格式化为 LLM 可读的 markdown 文本。

    用于 LLM 直调摘要的输入。保留关键内容原文，截断冗长的工具输出。
    """
    if not chunk and not previous_summary:
        return ""

    lines: list[str] = ["## 对话事件流", ""]
    if previous_summary:
        lines.append("### 之前的历史摘要（沿用）")
        lines.append(previous_summary)
        lines.append("")
    turn_idx = 0
    for ev in chunk:
        t = ev.get("type", "")
        d = ev.get("data") or {}
        if t == "USER_INPUT":
            turn_idx += 1
            content = d.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(p.get("data", "") or "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            lines.append(f"### 第 {turn_idx} 轮")
            lines.append(f"**用户**：{content}")
        elif t == "message_complete":
            lines.append(f"**AI**：{d.get('content', '')}")
        elif t == "reasoning_complete":
            content = d.get("content", "")
            if content:
                lines.append(f"**思考**：{content}")
        elif t == "tool_call":
            name = d.get("name", "")
            args = d.get("arguments", {})
            args_str = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
            if len(args_str) > 300:
                args_str = args_str[:300] + "...[截断]"
            lines.append(f"**工具调用**：{name}({args_str})")
        elif t == "tool_result":
            result = d.get("result", "")
            if not isinstance(result, str):
                result = str(result)
            if len(result) > 500:
                result = result[:500] + f"...[截断 {len(result) - 500} 字符]"
            lines.append(f"**工具结果**：{result}")
        elif t == "context_compact":
            prev = d.get("summary", "")
            if prev:
                lines.append("")
                lines.append("### 之前的历史摘要（沿用）")
                lines.append(prev)
                lines.append("")
        # 其他类型（external_message / usage_update 等）忽略

    return "\n".join(lines).strip()


def get_cursor_index(events: list[dict]) -> int:
    """返回最新已启用 context_compact 事件之后的索引（即 tail 起点）。

    无已启用 context_compact 时返回 0（首次压缩，从头开始）。
    """
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("type") == "context_compact" and _compact_enabled(events[i]):
            return i + 1
    return 0


def get_previous_summary(events: list[dict]) -> str | None:
    """返回最新已启用 context_compact 事件的 summary（用于 anchored 摘要并入）。"""
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("type") == "context_compact" and _compact_enabled(events[i]):
            return ((events[i].get("data") or {}).get("summary") or None)
    return None


def get_pending_compact_index(events: list[dict]) -> int | None:
    """返回最新 pending context_compact 的索引。"""
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


def _count_user_turns(events: list[dict]) -> int:
    """统计一段事件里 USER_INPUT 的数量（= user-turn 数）。"""
    return sum(1 for ev in events if ev.get("type") == "USER_INPUT")
