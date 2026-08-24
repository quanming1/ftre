"""Messaging route Hook contract.

MessageBus owns transport correlation and publishes one normalized route
boundary. Command and Inbox Plugins decide whether they handle a message;
Agent Runtime does not inspect either product concept.
"""
# Messaging route Hook 契约（由 Messaging Owner 定义，Kernel 不认识它——PRD-F14 §8）。
# MessageBus 负责传输相关性，发布唯一的规范化 route 边界；
# Command Plugin 与 Inbox Package 各自决定是否处理某条消息；
# Agent Runtime 不检查 Command/Queue 概念，只消费最终交付的 InboundMessage。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ftre.kernel.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec

from .message import BusMessage


@dataclass(frozen=True, slots=True)
class IngressResult:
    """The existing admission ACK shape, moved to the Messaging Owner."""
    # 接入裁决结果（ACK 形态）：由 Messaging Owner 统一持有，
    # 供 Command/Inbox 监听器返回"是否接受、落在哪个 session、request_id 是多少"。

    accepted: bool       # 是否接受该消息
    session_id: str      # 目标 session
    request_id: str = "" # 幂等请求 ID（client 提供）
    created: bool = False  # 是否新建了 session
    error: dict[str, Any] | None = None  # 拒绝原因（accepted=False 时携带）


async def _unhandled(_message: BusMessage) -> IngressResult | None:
    # 默认实现：无人处理 → 返回 None，表示"无消费者接管"。
    # 调用方据此给出稳定 capability error，而不是静默吞掉。
    return None


# messaging/route：WATERFALL 控制型 Hook——
#   - 监听器按注册顺序尝试接管消息，返回 IngressResult 即短路（后续监听器不再执行）；
#   - 全部返回 None 表示无消费者（如未装 Inbox 时普通输入 → capability error）；
#   - failure_policy=PROPAGATE：监听器异常向上抛，不静默吞错（接入口是关键路径）。
MESSAGING_ROUTE_SPEC = HookSpec(
    "messaging/route",
    "messaging",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=BusMessage,
    result_type=IngressResult | type(None),
    default=_unhandled,
    scope=HookScope.GLOBAL,
)


__all__ = ["MESSAGING_ROUTE_SPEC", "IngressResult"]
