"""
CompactionService — 上下文压缩 Service 的唯一真实实现

设计：
- inbox/before-claim 在领取下一条请求前做强制水位检查并等待压缩
- /compact 手动：立即压缩
- /compress-fast：零 LLM 成本裁剪旧 ToolResultBlock 输出

每次压缩：从上一个 compact 摘要 Msg 到现在，按估算 token 数切成多个内容块，每个块
由一个 LLM 摘要，再由本地确定性逻辑合并。摘要作为一条 role=user、name=compact
的 Msg 追加到 messages 数组（由 SessionProjection 投影 context_compact_done 落盘），
原始 Msg 永不删除。下一轮 LLM 上下文从最后一条 compact Msg 开始。
CompactionService 不直接写 state、不直接派发 WebSocket，全部通过注入的
``emit_maintenance`` Host sink 完成。快速压缩直接更新旧 Msg 中的工具结果块。

并发安全：
- 每个 session 同一时间最多只有一个真正的压缩 Task。
- 后来的手动或关键路径压缩请求不创建新任务，统一等待已有 Task。
- 等待者取消不会中断共享压缩；只有 Gateway 关闭时才强制取消。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

from ftre_agent.message import Msg, MsgName, TextBlock, ToolResultBlock
from ftre_llm import LlmCallConfig, LlmCredentials, LLMError, LlmRequest

from .config import CompactionConfig, parse_compaction_config
from .events import CompactEventName

logger = logging.getLogger(__name__)

DEFAULT_COMPACT_THRESHOLD = 0.8

# compress-fast 默认保护最近 N 轮对话内的工具输出不被裁剪（0 = 全裁）
DEFAULT_FAST_KEEP_TURNS = 0


def _select_compact_llm(config, compaction_config: CompactionConfig):
    """返回摘要实际使用的模型。

    专用摘要模型属于压缩包配置；没有配置时必须回退到“本轮请求”的主
    Agent 模型，而不是重新调用全局 default。这样切换 Agent 或模型后，
    压缩预算和摘要模型仍与当前 Turn 对齐。
    """
    return compaction_config.llm or config.llm


_SUMMARY_SECTION_ORDER = (
    "primary_request_and_intent",
    "key_technical_concepts",
    "files_and_code_sections",
    "errors_and_fixes",
    "problem_solving",
    "all_user_messages",
    "pending_tasks",
    "current_work",
    "next_step",
)
# `all_user_messages` 只是原始历史的机械投影，不需要 LLM 做语义推理。
# LLM 只输出下面这些需要归纳的节点，完整摘要仍由本地合并器补齐全部节点。
_LLM_SUMMARY_SECTION_ORDER = tuple(
    section for section in _SUMMARY_SECTION_ORDER if section != "all_user_messages"
)

COMPACT_CHUNK_LLM_SYSTEM_PROMPT = """\
你是对话上下文压缩流水线中的一个分块摘要 Worker。你只会看到整个会话的一段连续内容，
输出会被宿主程序按原始块顺序合并到下一个 Agent 使用的锚定摘要中。只输出
state_snapshot XML 节点，不要输出解释、Markdown 围栏或 analysis。只保留当前分块能证明的
事实；不要回答对话中的问题，不要编造不存在的文件、错误或任务。\
"""


class CompactionService:
    """上下文压缩 Service（全异步），由本包 Plugin 唯一创建。

    Service 只依赖 ftre 的公开 Session 事件和 LLM 配置对象。它不认识
    Inbox worker、TurnExecutor 或 Gateway；这些对象通过 Hook/Command 在
    外层调用它。这个边界是“卸载压缩包后核心仍可运行”的关键。
    """

    def __init__(
        self,
        *,
        session_manager,
        llm=None,
        emit_maintenance=None,
        threshold: float = DEFAULT_COMPACT_THRESHOLD,
        config_service=None,
        default_config: CompactionConfig | None = None,
    ):
        self.session_manager = session_manager
        self._llm = llm
        self._emit_maintenance = emit_maintenance or self._noop_maintenance
        self._config_service = config_service
        self._default_config = default_config or CompactionConfig(
            compact_threshold=threshold
        )
        self._progress_generation: dict[str, int] = {}

        # session_id → 真正执行 _do_compact 的共享 Task。
        # 这里只保存压缩本体，不保存后台调度的包装 Task，避免任务取消自己。
        self._compact_tasks: dict[str, asyncio.Task[str | None]] = {}

    async def _noop_maintenance(
        self, _session_id: str, _channel_id: str, _name: str, _value: dict
    ) -> None:
        return None

    def progress_generation(self, session_id: str) -> int:
        """Return the in-process generation advanced by durable compaction progress."""
        return self._progress_generation.get(session_id, 0)

    def _mark_progress(self, session_id: str) -> None:
        self._progress_generation[session_id] = self.progress_generation(session_id) + 1

    def config_for(self, _agent_config) -> CompactionConfig:
        """在 Hook/Command 边界读取最新配置，生成不可变快照。

        ``_agent_config`` 只保留在方法签名中用于表达调用边界：压缩配置从
        本包注入的 ConfigService 读取，主 LLM 的 context_window/max_output
        则继续从传入的 AgentConfig 读取。两类配置不能混成一个 Owner。
        """
        raw = {}
        if self._config_service is not None:
            snapshot = self._config_service.snapshot()
            raw = snapshot.value if snapshot is not None else {}
        return parse_compaction_config(raw, defaults=self._default_config)

    async def compact_if_needed(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        threshold: float | None = None,
        extra_tokens: int = 0,
        trigger: Literal["auto", "manual"] = "auto",
    ) -> bool:
        """按水位判断，必要时执行一次自动压缩。

        这是 Hook 使用的组合入口：先只读判断，再等待共享压缩 Task，最后用
        progress generation 判断是否真的产生持久化进展。手动命令使用
        compact_now，不经过这个阈值门控。
        """
        compaction_config = self.config_for(config)
        needed = await self.should_compact(
            session_id,
            channel_id,
            config,
            threshold=threshold,
            extra_tokens=extra_tokens,
            compaction_config=compaction_config,
        )
        if not needed:
            return False
        before = self.progress_generation(session_id)
        await self.compact(
            session_id,
            channel_id,
            config=config,
            trigger=trigger,
            compaction_config=compaction_config,
        )
        return self.progress_generation(session_id) > before

    async def compact_now(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        focus_hint: str = "",
    ) -> str | None:
        """Command Plane 的显式维护入口，直接触发一次摘要压缩。"""
        return await self.compact(
            session_id,
            channel_id,
            config=config,
            trigger="manual",
            focus_hint=focus_hint,
        )

    # ─── 只读判断 ──────────────────────────────────────────────────

    async def should_compact(
        self,
        session_id: str,
        channel_id: str,
        config,
        *,
        threshold: float | None = None,
        extra_tokens: int = 0,
        compaction_config: CompactionConfig | None = None,
    ) -> bool:
        """水位是否超过 threshold？只读 DB，不调 LLM。

        优先用 API 报告的真实 token（last_call_usage + pending 策略），
        全新 session 无 last_call_usage 时退化为字符估算。
        """
        compaction_config = compaction_config or self.config_for(config)
        threshold = (
            threshold
            if threshold is not None
            else compaction_config.compact_threshold
        )
        cw = getattr(config.llm, "context_window", None)
        if not cw or cw <= 0:
            return False
        max_output = max(0, getattr(config.llm, "max_output", None) or 0)
        safety_buffer = max(0, compaction_config.safety_buffer)
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
        compaction_config: CompactionConfig | None = None,
    ) -> str | None:
        """执行或等待该 session 当前唯一的压缩任务。

        创建 Task 与写入字典之间没有 ``await``，在 asyncio 单线程事件循环中
        是原子的。``shield`` 保证某个等待者被取消时，共享压缩仍继续运行。

        ``focus_hint`` 为用户在 ``/compact`` 后附带的自然语言提示词，透传给
        摘要 LLM，强调必须优先完整保留的上下文（如「登录模块相关代码」）。
        """
        compaction_config = compaction_config or self.config_for(config)
        task, created = self._get_or_create_compact_task(
            session_id,
            channel_id,
            config=config,
            trigger=trigger,
            focus_hint=focus_hint,
            compaction_config=compaction_config,
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
        compaction_config: CompactionConfig | None = None,
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
                compaction_config=compaction_config or self.config_for(config),
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
        from ftre.services.session.message import estimate_messages_tokens
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
        await self._emit_maintenance(
            session_id,
            channel_id,
            CompactEventName.DONE,
            {
                "mode": "fast",
                "messages": len(changed_messages),
                "tool_results": len(tool_results),
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            },
        )

        logger.info(
            f"[compact-fast] session={session_id} 裁剪 {len(tool_results)} 个工具结果, "
            f"tokens {tokens_before} → {tokens_after}"
        )
        self._mark_progress(session_id)
        return True

    async def _do_compact(
        self,
        session_id: str,
        channel_id: str,
        *,
        config,
        trigger: Literal["auto", "manual"] = "auto",
        focus_hint: str = "",
        compaction_config: CompactionConfig | None = None,
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

        compaction_config = compaction_config or self.config_for(config)

        # 4. 按 token 估算切块；事件里的块数与实际 LLM 调度使用同一份快照。
        chunks = _chunk_messages_by_tokens(
            head_messages,
            compaction_config.chunk_tokens,
        )
        from ftre.services.session.message import estimate_messages_tokens

        chunk_sizes = [estimate_messages_tokens(chunk) for chunk in chunks]

        # 5. 通知 Host 开始（不持久化）。模型必须显式随事件传递，
        #    不能让客户端从上一条 assistant 回复推断压缩实际使用的模型。
        compact_llm = _select_compact_llm(config, compaction_config)
        logger.info(
            "[compact] session=%s prepared chunks=%s target_tokens=%s sizes=%s "
            "parallelism=%s model=%s api_type=%s",
            session_id,
            len(chunks),
            compaction_config.chunk_tokens,
            chunk_sizes,
            compaction_config.chunk_parallelism,
            compact_llm.model,
            getattr(compact_llm, "api_type", "unknown"),
        )
        await self._emit_maintenance(
            session_id,
            channel_id,
            CompactEventName.START,
            {
                "messages": len(head_messages),
                "tokens": tokens_before,
                "model": compact_llm.model,
                "mode": "token_chunks",
                "chunks": len(chunks),
                "chunk_tokens": compaction_config.chunk_tokens,
                "parallelism": compaction_config.chunk_parallelism,
                "user_messages": len(_extract_user_message_texts(head_messages)),
            },
        )

        # 6. LLM 直调摘要（previous_summary 只交给首块，避免重复输入）
        summary = await self._run_compact_llm(
            head_messages, config=config, previous_summary=previous_summary,
            session_id=session_id, focus_hint=focus_hint,
            compaction_config=compaction_config,
            chunks=chunks,
        )
        if not summary:
            # 摘要为空（模型只输出思考、接口异常等）：默认重试一次
            logger.warning(f"[compact] session={session_id} 摘要为空，重试一次")
            summary = await self._run_compact_llm(
                head_messages, config=config, previous_summary=previous_summary,
                session_id=session_id, focus_hint=focus_hint,
                compaction_config=compaction_config,
                chunks=chunks,
            )
        if not summary:
            # 重试仍失败：回退 compress_fast 兜底，避免直接放弃导致 Lane BLOCKED。
            # （auto 路径的 Hook 也会再跑一次 compress_fast，但那是幂等
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

        # 7. 估算摘要后 token
        from ftre.services.session.message import estimate_messages_tokens
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

        # 8. 发 context_compact_done：Host Projection 将其投影为 user/compact Msg
        #    value 含完整 summary_text（非预览），持久化由 Projection 完成。
        await self._emit_maintenance(
            session_id,
            channel_id,
            CompactEventName.DONE,
            {
                "summary_text": summary,
                "through_message_id": through_message_id,
                "trigger": trigger,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "mode": "summary",
            },
        )
        self._mark_progress(session_id)

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
        compaction_config: CompactionConfig | None = None,
        chunks: list[list[dict]] | None = None,
    ) -> str | None:
        """按 token 分块调用 LLM，再按原始块顺序确定性合并。"""
        try:
            snapshot = compaction_config or self.config_for(config)
            chunk_records = chunks or _chunk_messages_by_tokens(
                head_messages,
                snapshot.chunk_tokens,
            )
            if not chunk_records:
                logger.debug("[compact] 消息文本为空，跳过 LLM 调用")
                return None

            concurrency = max(1, min(8, snapshot.chunk_parallelism))
            retry_attempts = max(0, min(2, snapshot.chunk_retry_attempts))
            timeout_seconds = max(5.0, min(600.0, snapshot.chunk_timeout_seconds))
            semaphore = asyncio.Semaphore(concurrency)

            async def run_chunk(
                index: int,
                records: list[dict],
            ) -> tuple[int, dict[str, str] | None]:
                async with semaphore:
                    context = _serialize_messages(records)
                    if not context.strip():
                        return index, None
                    chunk_tokens = _estimate_chunk_tokens(records)
                    llm_cfg = _select_compact_llm(config, snapshot)
                    for attempt in range(retry_attempts + 1):
                        started = asyncio.get_running_loop().time()
                        logger.info(
                            "[compact] session=%s chunk=%s/%s call tokens=%s model=%s "
                            "api_type=%s attempt=%s/%s",
                            session_id,
                            index + 1,
                            len(chunk_records),
                            chunk_tokens,
                            llm_cfg.model,
                            llm_cfg.api_type,
                            attempt + 1,
                            retry_attempts + 1,
                        )
                        try:
                            result = await asyncio.wait_for(
                                self._run_summary_chunk(
                                    index,
                                    context=context,
                                    previous_summary=previous_summary if index == 0 else None,
                                    config=config,
                                    focus_hint=focus_hint,
                                    compaction_config=snapshot,
                                ),
                                timeout=timeout_seconds,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - one part must not cancel siblings
                            logger.warning(
                                "[compact] session=%s chunk=%s/%s failed tokens=%s model=%s "
                                "attempt=%s/%s: %s",
                                session_id,
                                index + 1,
                                len(chunk_records),
                                chunk_tokens,
                                llm_cfg.model,
                                attempt + 1,
                                retry_attempts + 1,
                                exc,
                            )
                            result = None
                        elapsed = asyncio.get_running_loop().time() - started
                        if result:
                            logger.info(
                                "[compact] session=%s chunk=%s/%s completed tokens=%s "
                                "model=%s attempt=%s elapsed=%.2fs",
                                session_id,
                                index + 1,
                                len(chunk_records),
                                chunk_tokens,
                                llm_cfg.model,
                                attempt + 1,
                                elapsed,
                            )
                            return index, result
                        logger.warning(
                            "[compact] session=%s chunk=%s/%s empty/failed tokens=%s "
                            "model=%s attempt=%s/%s elapsed=%.2fs",
                            session_id,
                            index + 1,
                            len(chunk_records),
                            chunk_tokens,
                            llm_cfg.model,
                            attempt + 1,
                            retry_attempts + 1,
                            elapsed,
                        )
                return index, None

            results = await asyncio.gather(
                *(run_chunk(index, records) for index, records in enumerate(chunk_records)),
                return_exceptions=True,
            )
            summaries: list[dict[str, str]] = []
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    logger.warning("[compact] chunk task failed: %s", result)
                    return None
                index, sections = result
                if sections is None:
                    logger.warning("[compact] chunk=%s produced no sections", index)
                    return None
                summaries.append(sections)

            summary = _merge_chunk_summaries(
                summaries,
                user_messages=_build_user_messages_section(
                    head_messages,
                    previous_summary=previous_summary,
                ),
            )
            if not summary:
                logger.warning("[compact] chunk summary merge produced no content")
                return None
            return summary
        except asyncio.CancelledError:
            raise
        except LLMError as exc:
            logger.warning(
                "[compact] 并行摘要失败 code=%s message=%s", exc.code, exc.message
            )
            return None
        except Exception:
            logger.exception("[compact] 分块摘要异常")
            return None

    async def _run_summary_chunk(
        self,
        index: int,
        *,
        context: str,
        previous_summary: str | None,
        config,
        focus_hint: str,
        compaction_config: CompactionConfig,
    ) -> dict[str, str] | None:
        """运行一个 token chunk；不写 Session、不发事件。"""
        prompt_parts = _build_prompt(
            previous_summary=previous_summary,
            context=[context],
            min_chars=max(120, min(12_000, _estimate_body_chars(context) // 20)),
            focus_hint=focus_hint,
        )
        prompt_parts[-1] += (
            "\n\n本次只总结当前内容块。可以输出以下 XML 节点，节点内容只填写当前块中确认的事实；"
            "没有内容的节点可以留空：\n"
            + "\n".join(f"<{section}>...</{section}>" for section in _LLM_SUMMARY_SECTION_ORDER)
            + "\n不要输出当前块之外的推断。"
        )
        messages = [
            {"role": "system", "content": COMPACT_CHUNK_LLM_SYSTEM_PROMPT},
            *[{"role": "user", "content": part} for part in prompt_parts],
        ]

        llm_cfg = _select_compact_llm(config, compaction_config)
        if self._llm is None:
            logger.warning("[compact] llm Service 未注入，跳过摘要 chunk=%s", index)
            return None
        provider = getattr(llm_cfg, "provider", "")
        if not isinstance(provider, str) or not provider.strip():
            logger.warning("[compact] LLM 配置缺少 provider，跳过摘要 chunk=%s", index)
            return None
        call_config = LlmCallConfig(
            provider=provider,
            model=llm_cfg.model,
            api_type=llm_cfg.api_type,
            max_tokens=_summary_chunk_max_tokens(llm_cfg),
            temperature=0.0,
            reasoning_effort=llm_cfg.reasoning_effort,
        )
        credentials = LlmCredentials(
            api_key=llm_cfg.api_key,
            api_base=llm_cfg.api_base,
        )
        request = LlmRequest.from_parts(
            call_config,
            messages,
            purpose="compaction",
        )

        collected: list[str] = []
        try:
            async for chunk in self._llm.stream(request, credentials=credentials):
                if getattr(chunk, "type", "") == "text-delta":
                    collected.append(chunk.text)
        except LLMError as exc:
            logger.warning(
                "[compact] chunk=%s LLM failed code=%s message=%s",
                index,
                exc.code,
                exc.message,
            )
            return None

        return _parse_chunk_sections("".join(collected))
    # ─── 工具方法 ──────────────────────────────────────────────────

    async def _emit_failed(self, session_id: str, channel_id: str, reason: str) -> None:
        """发 context_compact_failed Host maintenance record（不持久化）。"""
        try:
            await self._emit_maintenance(
                session_id,
                channel_id,
                CompactEventName.FAILED,
                {"reason": reason},
            )
        except Exception:  # noqa: BLE001 - 事件出口失败不能掩盖压缩任务本身的结果
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

    async def close(self) -> None:
        """卸载前取消并等待所有 in-flight 压缩，保证资源可逆。"""
        await self.cancel_all_compact_tasks()


# ─── 模块级纯函数（可单测） ───────────────────────────────────────────


def _summary_chunk_max_tokens(llm_config) -> int:
    """为每个 chunk 设置独立输出预算，避免单块摘要无限膨胀。"""
    configured = getattr(llm_config, "max_output", None)
    if isinstance(configured, int) and configured > 0:
        return max(512, min(8192, configured))
    return 4096


def _parse_chunk_sections(raw: str) -> dict[str, str] | None:
    """解析一个 chunk 的局部 state_snapshot；缺失节点按空正文处理。"""
    clean = re.sub(r"```(?:xml)?|```", "", raw or "", flags=re.IGNORECASE).strip()
    clean = re.sub(
        r"<analysis>.*?</analysis>",
        "",
        clean,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    sections: dict[str, str] = {}
    for section in _LLM_SUMMARY_SECTION_ORDER:
        match = re.search(
            rf"<{re.escape(section)}>(.*?)</{re.escape(section)}>",
            clean,
            flags=re.DOTALL | re.IGNORECASE,
        )
        sections[section] = match.group(1).strip() if match else ""
    if not any(value for value in sections.values()):
        return None
    return sections


def _merge_chunk_summaries(
    summaries: list[dict[str, str]],
    *,
    user_messages: str = "",
) -> str | None:
    """按 chunk 顺序合并节点，并确定性补齐真实用户消息清单。"""
    if not summaries or not any(
        part.get(section, "").strip()
        for part in summaries
        for section in _SUMMARY_SECTION_ORDER
    ):
        return None
    lines = ["<state_snapshot>"]
    for section in _SUMMARY_SECTION_ORDER:
        if section == "all_user_messages":
            values = [user_messages.strip()] if user_messages.strip() else []
        else:
            values = [part.get(section, "").strip() for part in summaries]
        values = [value for value in values if value]
        lines.extend([
            f"  <{section}>",
            "\n\n".join(values),
            f"  </{section}>",
        ])
    lines.append("</state_snapshot>")
    return "\n".join(lines)


def _extract_user_message_texts(messages: list[dict]) -> list[str]:
    """提取当前快照中的真实用户消息，跳过压缩摘要和展示气泡。"""
    texts: list[str] = []
    for record in messages:
        if record.get("role") != "user":
            continue
        name = record.get("name", MsgName.DEFAULT.value)
        if str(name) != MsgName.DEFAULT.value:
            continue
        metadata = record.get("metadata")
        if isinstance(metadata, dict) and metadata.get("hide"):
            continue
        text = Msg.model_validate(record).get_text_content() or ""
        if text.strip():
            texts.append(text.strip())
    return texts


def _build_user_messages_section(
    messages: list[dict],
    *,
    previous_summary: str | None = None,
) -> str:
    """代码生成 `all_user_messages`，避免让 LLM 机械复述用户输入。

    增量压缩时，历史快照已经被上一条 compact 摘要替代，因此先读取上一份摘要中的
    同名节点，再追加本次 tail 的真实 UserMsg；这样不会因为分块压缩丢掉早期用户消息。
    """
    values: list[str] = []
    if previous_summary:
        match = re.search(
            r"<all_user_messages>(.*?)</all_user_messages>",
            previous_summary,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if match and match.group(1).strip():
            values.append(match.group(1).strip())
    values.extend(
        f"用户消息 {index}: {text}"
        for index, text in enumerate(_extract_user_message_texts(messages), start=1)
    )
    return "\n\n".join(values)


def _chunk_messages_by_tokens(
    messages: list[dict],
    chunk_tokens: int,
) -> list[list[dict]]:
    """按稳定的 Msg 边界切分上下文，单条超限 Msg 独立成为 oversized chunk。"""
    from ftre.services.session.message import estimate_messages_tokens

    limit = max(1, int(chunk_tokens))
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0
    for record in messages:
        record_tokens = estimate_messages_tokens([record])
        if current and current_tokens + record_tokens > limit:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(record)
        current_tokens += record_tokens
    if current:
        chunks.append(current)
    return chunks


def _estimate_chunk_tokens(records: list[dict]) -> int:
    """估算单个 chunk 的 token 数，供诊断日志复用同一估算器。"""
    from ftre.services.session.message import estimate_messages_tokens

    return estimate_messages_tokens(records)


def _serialize_messages(
    chunk: list[dict],
    *,
    tool_output_max_chars: int = 2000,
) -> str:
    """把 Msg 历史序列化为 LLM 可读的纯文本。"""
    from ftre.services.session.message import to_openai

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
    return len(body)


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
