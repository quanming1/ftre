"""SessionLane 的上下文水位门。

ContextGate 只回答“现在能否安全开始下一轮、是否必须先压缩”。它不持有队列、
不创建 Turn，也不向客户端发事件；因此压缩与执行的次序只能由 SessionLane 决定。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ftre.services.agent.config import AgentConfig
from ftre.services.session.entity.state import QueueItem

from ..compaction.manager import CompactManager


@dataclass(frozen=True)
class ContextDecision:
    action: Literal["pass", "compact", "block"]
    reason: str = ""


class ContextGate:
    """统一实现 70% 轮后预压缩和 80% 领取前强制压缩策略。"""

    def __init__(self, compact_manager: CompactManager) -> None:
        self._compact = compact_manager

    async def before_claim(
        self,
        session_id: str,
        channel_id: str,
        config: AgentConfig,
        item: QueueItem,
    ) -> ContextDecision:
        """队首仍在 pending 时检查；失败不会污染这条用户消息的上下文。"""
        # 80% 是领取前硬门槛：即使上一轮未触发压缩，下一条消息估算后也可能越过安全线。
        extra_tokens = max(1, len(item.content) // 3)
        # 强制线使用当前 agent 配置，而非固定常量；不同 agent 可以有不同上下文窗口。
        threshold = float(getattr(config.context, "compact_threshold", 0.8))
        needed = await self._compact.should_compact(
            session_id,
            channel_id,
            config,
            threshold=threshold,
            extra_tokens=extra_tokens,
        )
        return ContextDecision("compact", "领取下一条前达到强制水位") if needed else ContextDecision("pass")

    async def after_turn(
        self, session_id: str, channel_id: str, config: AgentConfig
    ) -> ContextDecision:
        """一轮完整收尾后检查；等待消息会在这个关口后才可被领取。"""
        # 70% 是轮次之间的预压缩，把压缩放在两轮之间，避免它与 Turn 并发。
        threshold = float(getattr(config.context, "precompact_threshold", 0.7))
        needed = await self._compact.should_compact(
            session_id, channel_id, config, threshold=threshold
        )
        return ContextDecision("compact", "本轮结束后达到预压缩水位") if needed else ContextDecision("pass")

    async def compact(
        self, session_id: str, channel_id: str, config: AgentConfig
    ) -> ContextDecision:
        """等待压缩并复核安全水位；摘要无效时尝试一次无 LLM 的快速裁剪。"""
        # 压缩失败不能静默放行；复核仍超硬水位时 BLOCKED，并保留队首等待处理。
        try:
            await self._compact.compact(
                session_id, channel_id, config=config, trigger="auto"
            )
        except Exception as exc:  # Lane 决定是否进入 BLOCKED，而非吞掉错误。  # noqa: BLE001 legacy compatibility boundary reviewed in F1
            return ContextDecision("block", f"上下文压缩失败: {exc}")
        hard_threshold = float(getattr(config.context, "compact_threshold", 0.8))
        still_over = await self._compact.should_compact(
            session_id, channel_id, config, threshold=hard_threshold
        )
        if not still_over:
            return ContextDecision("pass")
        try:
            await self._compact.compress_fast(session_id, channel_id, config=config)
        except Exception as exc:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
            return ContextDecision("block", f"快速裁剪失败: {exc}")
        if await self._compact.should_compact(
            session_id, channel_id, config, threshold=hard_threshold
        ):
            return ContextDecision("block", "压缩后上下文仍超过安全水位")
        return ContextDecision("pass")

    async def cancel(self, session_id: str) -> bool:
        """只有会话关闭/网关停止可以取消共享压缩；普通 /cancel 不触碰它。"""
        return await self._compact.cancel_compact(session_id)
