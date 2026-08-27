"""压缩包拥有的 Agent/Inbox Hook。

压缩包通过 ``inbox/before-claim`` 负责交付前水位门控，通过
``agent/after-run`` 做轮后维护，通过 ``agent/run-error`` 做 overflow 恢复。
它不接触 Inbox 私有 Repository，也不需要知道 Agent worker 如何调度。
"""
# 压缩包（ftre-compaction）的 Hook 注册层——本文件是压缩包与 ftre Host 之间的
# 唯一"时机接线"点，符合 PRD-F14 §4.4/§8：Compaction 只通过公开 HookSpec 与
# 公开 Service 工作，不 import Host 私有 Runtime/Repository。
#
# 三个 Hook 的分工（对应 §8 数据流中的"Compaction 仅通过 Hook 工作"）：
#   1. inbox/before-claim   —— 交付前水位门控：领取队首前检查水位，超线先压缩；
#   2. agent/after-run      —— 轮后维护：每轮结束按 precompact 阈值预压缩；
#   3. agent/run-error      —— overflow 恢复：LLM 报上下文溢出时强制压缩并请求重试。
#
# 边界约定：
#   - 每个 Hook 都拿到"本轮 Agent 的 AgentConfig"，但压缩专属阈值/安全余量
#     来自 CompactionService.config_for()（本包注入的 ConfigService），
#     两类配置不混成一个 Owner；
#   - 监听器全部 all_agent_scopes=True：不依赖 Agent scope，任何 session 都触发；
#   - 返回的 receipts 交给 Plugin effect 管理，卸载时全部摘除（可逆）。

from __future__ import annotations

import asyncio
import logging

from ftre_agent import (
    AGENT_AFTER_RUN_SPEC,
    AGENT_RUN_ERROR_SPEC,
    RequestErrorPayload,
    RetryRequest,
)
from ftre_inbox.hooks import INBOX_BEFORE_CLAIM_SPEC, EnterClaim, RejectClaim

from ftre.services.agent.config import load_config

logger = logging.getLogger(__name__)


def register_hooks(ctx, service) -> list[object]:
    """注册三个 Hook，并把返回的 Receipt 交给 Plugin effect 管理。

    每个 Hook 都拿到当前调用的 AgentConfig，但阈值等压缩专属字段来自
    ``CompactionService.config_for``。这样多 Agent 场景仍使用本轮 Agent 的
    LLM 预算，同时配置热更新只影响下一次 Hook 调用。
    """
    # ── 公共压缩执行体：水位判断 → 置维护标记 → 压缩 → 复核 ──
    # 被 on_after_turn 与 on_inbox_before_claim 共用，保证三处行为一致。
    async def run_compaction(payload, *, threshold: float, extra_tokens: int = 0) -> bool:
        # 取消信号已置位：不执行任何压缩动作（尊重调用方取消语义）
        if payload.cancellation.is_set():
            return False
        # 在本次 Hook 开始时固定快照；压缩和重新检查使用同一套阈值，避免
        # 热更新恰好发生在 LLM 调用中途导致"前后两套策略"。
        compaction_config = service.config_for(payload.config)
        # 第一步：只读水位判断（不调 LLM、不写库）
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
        # 第二步：置维护标记（让 AgentLoop 知道 session 处于 compacting，
        # 对外表现为"忙碌"而非 idle，避免并发领取/执行与压缩竞争）
        mark = payload.set_maintenance
        marked = False
        try:
            if mark is not None:
                await mark(True, "context compaction")
                marked = True
            # 第三步：执行/等待共享压缩任务（同 session 并发调用会复用同一 Task）
            await service.compact(
                payload.session_id,
                payload.channel_id,
                config=payload.config,
                trigger="auto",
                compaction_config=compaction_config,
            )
            # 第四步：压缩后复核水位——仍超线说明压缩没产生足够空间
            #（如摘要 LLM 失败），按失败处理而不是静默通过。
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
            # 压缩中被打断：主动取消共享压缩任务，避免遗留半个压缩状态
            await service.cancel_compact(payload.session_id)
            raise
        finally:
            # 无论成败都要解除维护标记，让 session 恢复 idle
            if mark is not None and marked:
                await mark(False, "")

    # ── Hook 1：agent/after-run（轮后预压缩）──
    # 语义：一轮 Turn 完整结束后触发。阈值用 precompact_threshold（默认 0.7），
    # 即"两轮之间的提前压缩"：把压缩放在轮间，避免与下一轮执行并发。
    async def on_after_turn(payload, next_):
        # 取消/无配置/无通道时不干预，直接短路到 next_（fail-open）
        if payload.cancellation.is_set() or payload.config is None or not payload.channel_id:
            return await next_()
        compaction_config = service.config_for(payload.config)
        await run_compaction(
            payload,
            threshold=compaction_config.precompact_threshold,
        )
        # WATERFALL 语义：本监听器不吞掉后续监听器，执行完继续走 next_
        return await next_()

    # ── Hook 2：agent/run-error（overflow 恢复）──
    # 语义：LLM/Tool 请求失败时触发。仅当错误码命中上下文溢出（overflow/
    # context_length/too_long）时介入：强制压缩（threshold=0 无条件压），
    # 压缩产生持久化进展后返回 RetryRequest 请求重试一次。
    async def on_request_error(payload: RequestErrorPayload, next_):
        # 非溢出错误不归压缩管：交给下一个监听器（或默认策略）
        if payload.cancellation.is_set() or not _is_overflow(payload.error_code):
            return await next_()
        try:
            # payload 可能没带 config（错误发生在配置解析前），用全局配置兜底
            config_value = payload.config or load_config()
            # channel_id 缺失时从 session 元数据补取（压缩事件需要落 channel）
            session = await ctx.sessions.get_session(payload.session_id)
            channel_id = payload.channel_id or str((session or {}).get("channel_id", ""))
            # progress generation 是"持久化压缩是否真正发生"的进程内代币：
            # 压缩完成一次 +1，用它判断本次恢复是否真的腾出了空间
            before = service.progress_generation(payload.session_id)
            await service.compact_if_needed(
                payload.session_id,
                channel_id,
                config=config_value,
                threshold=0.0,  # 溢出场景无条件压，不再按水位判断
                trigger="auto",
            )
            after = service.progress_generation(payload.session_id)
            # 只有"压缩确实发生"才请求重试；没压成（或中途取消）则放弃，
            # 把原始错误继续交给 next_（保持原错误语义）
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
            # 恢复失败绝不掩盖原始请求错误：记 warning 后继续 next_
            logger.warning(
                "[compaction] overflow recovery failed session=%s: %s",
                payload.session_id,
                exc,
            )
        return await next_()

    # ── Hook 3：inbox/before-claim（交付前水位门控）──
    # 语义：Inbox Package 在领取队首前触发。若水位超线先压缩再放行；
    # 任何失败都返回 RejectClaim("keep")——队首保留 pending，下次再试，
    # 绝不丢弃用户消息。
    async def on_inbox_before_claim(payload, _next_):
        """Inbox Package 的领取前门控；失败时保留 pending。"""
        config_value = load_config()
        try:
            # 领取前用强制阈值（compact_threshold，默认 0.8）+ 队首消息估算：
            # 即使上一轮没触发预压缩，这条新消息也可能越过安全线
            needed = await service.should_compact(
                payload.session_id,
                payload.channel_id,
                config_value,
                threshold=service.config_for(config_value).compact_threshold,
                extra_tokens=max(1, len(payload.candidate.content) // 3),
                compaction_config=service.config_for(config_value),
            )
            if needed:
                await service.compact(
                    payload.session_id,
                    payload.channel_id,
                    config=config_value,
                    trigger="auto",
                    compaction_config=service.config_for(config_value),
                )
            # 放行：把队首 request_id 交还给 Inbox，允许 claim
            return EnterClaim(payload.candidate.request_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep pending on policy failure
            # 门控失败 = 不能安全交付：拒绝 claim 且保留 pending（keep），
            # 让消息留在队列里，而不是被丢弃或带病执行
            logger.warning("[compaction] inbox before-claim failed session=%s: %s", payload.session_id, exc)
            return RejectClaim("keep", f"上下文压缩失败：{exc}")

    # 注册三个 Hook；receipts 统一返回给 Plugin，由 ctx.effect 在卸载时摘除
    receipts = [
        ctx.hook_runtime.register(
            AGENT_AFTER_RUN_SPEC,
            on_after_turn,
            owner="ftre-compaction",
            context=ctx,
            all_agent_scopes=True,  # 不按 agent scope 隔离：所有 session 的轮后都参与
        ),
        ctx.hook_runtime.register(
            AGENT_RUN_ERROR_SPEC,
            on_request_error,
            owner="ftre-compaction",
            context=ctx,
            all_agent_scopes=True,
        ),
    ]
    receipts.append(
        ctx.hook_runtime.register(
            INBOX_BEFORE_CLAIM_SPEC,
            on_inbox_before_claim,
            owner="ftre-compaction",
            context=ctx,
            all_agent_scopes=True,
        )
    )
    return receipts


def _is_overflow(error_code: str) -> bool:
    """判断错误码是否属于上下文溢出类。

    覆盖三家 LLM 提供方的常见说法：overflow（OpenAI 风格）、
    context_length（Anthropic 风格）、too_long（其他风格）。
    大小写不敏感；错误码为空时返回 False。
    """
    value = (error_code or "").lower()
    return any(token in value for token in ("overflow", "context_length", "too_long"))


__all__ = ["register_hooks"]
