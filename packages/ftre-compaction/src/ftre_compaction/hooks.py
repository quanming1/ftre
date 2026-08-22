"""压缩包拥有的 Agent 语义 Hook。

Hook 是本包与 Agent 数据面的唯一连接点：核心只提供 ``pre-step``、
``after-turn`` 和 ``request-error`` 契约，压缩包在这些边界上决定何时检查
水位、何时等待压缩以及 overflow 后是否允许一次重试。Hook 不接触
MailboxStore，也不自己 claim pending；队列和串行化仍由 SessionLane 负责。
"""

from __future__ import annotations

import asyncio
import logging

from ftre.services.agent.config import load_config
from ftre.services.agent.hooks import (
    AGENT_AFTER_TURN_SPEC,
    AGENT_PRE_STEP_SPEC,
    AGENT_REQUEST_ERROR_SPEC,
    RejectStep,
    RequestErrorPayload,
    RetryRequest,
)

logger = logging.getLogger(__name__)


def register_hooks(ctx, service) -> list[object]:
    """注册三个 Hook，并把返回的 Receipt 交给 Plugin effect 管理。

    每个 Hook 都拿到当前调用的 AgentConfig，但阈值等压缩专属字段来自
    ``CompactionService.config_for``。这样多 Agent 场景仍使用本轮 Agent 的
    LLM 预算，同时配置热更新只影响下一次 Hook 调用。
    """

    async def run_compaction(payload, *, threshold: float, extra_tokens: int = 0) -> bool:
        if payload.cancellation.is_set():
            return False
        # 在本次 Hook 开始时固定快照；压缩和重新检查使用同一套阈值，避免
        # 热更新恰好发生在 LLM 调用中途导致“前后两套策略”。
        compaction_config = service.config_for(payload.config)
        needed = await service.should_compact(
            payload.session_id,
            payload.channel_id,
            payload.config,
            threshold=threshold,
            extra_tokens=extra_tokens,
            compaction_config=compaction_config,
        )
        if not needed:
            return False
        mark = payload.set_maintenance
        marked = False
        try:
            if mark is not None:
                await mark(True, "context compaction")
                marked = True
            await service.compact(
                payload.session_id,
                payload.channel_id,
                config=payload.config,
                trigger="auto",
                compaction_config=compaction_config,
            )
            if await service.should_compact(
                payload.session_id,
                payload.channel_id,
                payload.config,
                threshold=threshold,
                extra_tokens=extra_tokens,
                compaction_config=compaction_config,
            ):
                raise RuntimeError("压缩完成后仍超过当前上下文安全水位")
            return True
        except asyncio.CancelledError:
            await service.cancel_compact(payload.session_id)
            raise
        finally:
            if mark is not None and marked:
                await mark(False, "")

    async def on_pre_step(payload, next_):
        if payload.cancellation.is_set() or payload.config is None or not payload.channel_id:
            return await next_()
        try:
            compaction_config = service.config_for(payload.config)
            await run_compaction(
                payload,
                threshold=compaction_config.compact_threshold,
                extra_tokens=max(1, len(payload.candidate.content) // 3),
            )
        except Exception as exc:  # noqa: BLE001 - Hook owns the blocking policy
            logger.warning(
                "[compaction] pre-step failed session=%s: %s",
                payload.session_id,
                exc,
            )
            return RejectStep("keep", f"上下文压缩失败：{exc}")
        return await next_()

    async def on_after_turn(payload, next_):
        if payload.cancellation.is_set() or payload.config is None or not payload.channel_id:
            return await next_()
        compaction_config = service.config_for(payload.config)
        await run_compaction(
            payload,
            threshold=compaction_config.precompact_threshold,
        )
        return await next_()

    async def on_request_error(payload: RequestErrorPayload, next_):
        if payload.cancellation.is_set() or not _is_overflow(payload.error_code):
            return await next_()
        try:
            config_value = payload.config or load_config()
            session = await ctx.sessions.get_session(payload.session_id)
            channel_id = payload.channel_id or str((session or {}).get("channel_id", ""))
            before = service.progress_generation(payload.session_id)
            await service.compact_if_needed(
                payload.session_id,
                channel_id,
                config=config_value,
                threshold=0.0,
                trigger="auto",
            )
            after = service.progress_generation(payload.session_id)
            if not payload.cancellation.is_set() and after > before:
                return RetryRequest(
                    reason="上下文溢出后已完成持久化压缩",
                    progress_token=f"compaction:{payload.session_id}:{after}",
                    max_attempts=1,
                )
        except asyncio.CancelledError:
            await service.cancel_compact(payload.session_id)
            raise
        except Exception as exc:  # noqa: BLE001 - preserve original request error
            logger.warning(
                "[compaction] overflow recovery failed session=%s: %s",
                payload.session_id,
                exc,
            )
        return await next_()

    receipts = [
        ctx.hook_runtime.register(
            AGENT_PRE_STEP_SPEC,
            on_pre_step,
            owner="ftre-compaction",
            context=ctx,
            global_listener=True,
        ),
        ctx.hook_runtime.register(
            AGENT_AFTER_TURN_SPEC,
            on_after_turn,
            owner="ftre-compaction",
            context=ctx,
            global_listener=True,
        ),
        ctx.hook_runtime.register(
            AGENT_REQUEST_ERROR_SPEC,
            on_request_error,
            owner="ftre-compaction",
            context=ctx,
            global_listener=True,
        ),
    ]
    return receipts


def _is_overflow(error_code: str) -> bool:
    value = (error_code or "").lower()
    return any(token in value for token in ("overflow", "context_length", "too_long"))


__all__ = ["register_hooks"]
