"""Compaction Feature Plugin.

The Service owner lives in ``ftre.services.compaction``. This Feature only
registers semantic Agent Hooks against the injected Service.
"""

from __future__ import annotations

import logging

from cordis import Context

from ftre.services.agent.config import load_config
from ftre.services.agent.hooks import (
    AGENT_PRE_STEP_SPEC,
    AGENT_REQUEST_ERROR_SPEC,
    RequestErrorPayload,
    RetryRequest,
)

logger = logging.getLogger(__name__)

inject = ("compaction", "sessions", "hook_runtime")
provide = ()


def apply(ctx: Context, config=None):
    """Attach reversible pressure/recovery Hook listeners to the Service."""
    service = ctx.compaction

    async def on_pre_step(payload, next_):
        if payload.cancellation.is_set():
            return await next_()
        if payload.config is None or not payload.channel_id:
            return await next_()
        try:
            await service.compact_if_needed(
                payload.session_id,
                payload.channel_id,
                config=payload.config,
                threshold=float(
                    getattr(payload.config.context, "precompact_threshold", 0.7)
                ),
                extra_tokens=max(1, len(payload.candidate.content) // 3),
            )
        except Exception as exc:  # noqa: BLE001 - pressure optimization is fail-open
            logger.warning(
                "[compaction] pre-step pressure check failed session=%s: %s",
                payload.session_id,
                exc,
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
            if (
                not payload.cancellation.is_set()
                and after > before
            ):
                return RetryRequest(
                    reason="上下文溢出后已完成持久化压缩",
                    progress_token=f"compaction:{payload.session_id}:{after}",
                    max_attempts=1,
                )
        except Exception as exc:  # noqa: BLE001 - preserve original request error
            logger.warning(
                "[compaction] overflow recovery failed session=%s: %s",
                payload.session_id,
                exc,
            )
        return await next_()

    pre_receipt = ctx.hook_runtime.register(
        AGENT_PRE_STEP_SPEC,
        on_pre_step,
        owner="compaction",
        context=ctx,
        global_listener=True,
    )
    error_receipt = ctx.hook_runtime.register(
        AGENT_REQUEST_ERROR_SPEC,
        on_request_error,
        owner="compaction",
        context=ctx,
        global_listener=True,
    )
    ctx.effect(
        lambda: pre_receipt.dispose,
        label="hook:agent:pre-step:compaction",
    )
    ctx.effect(
        lambda: error_receipt.dispose,
        label="hook:agent:request-error:compaction",
    )

def _is_overflow(error_code: str) -> bool:
    value = (error_code or "").lower()
    return any(token in value for token in ("overflow", "context_length", "too_long"))


__all__ = ["apply", "inject", "provide"]
