"""Turn 私有状态模型（Runtime 内部，不进入公开契约）。

Turn 是一等公民：一个有状态的生命周期对象，从收到已交付输入到响应完成。
对外稳定结果是 ``ftre_agent.AgentRunResult``；这里的 ``TurnStatus`` 只描述
Runtime 内部状态机的阶段。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ftre_agent import AgentConfig, InboundMessage

if TYPE_CHECKING:
    from ftre_agent_core.agent import ReActAgent
    from ftre_agent_core.event import UserConfirmResultEvent


class TurnStatus(str, Enum):
    """Turn 生命周期的阶段。

    状态流转（正常路径）：
        BUILDING → RUNNING → FINALIZING → COMPLETED
    终态：COMPLETED（正常）/ CANCELLED（被取消）/ ERROR（异常）
    """

    BUILDING = "building"  # 鉴权 + 构建消息 + 创建 Agent
    RUNNING = "running"  # 驱动 Agent 执行，逐条投递事件
    FINALIZING = "finalizing"  # Turn 已运行完成，等待统一收尾
    COMPLETED = "completed"  # 正常完成（终态）
    CANCELLED = "cancelled"  # 被用户取消（终态）
    ERROR = "error"  # 执行异常（终态）


# Runtime 内部终态 → 公开 AgentRunResult.status 的唯一映射。
# 契约只有 completed/cancelled/failed 三个稳定值，中间状态不外泄。
PUBLIC_RUN_STATUS: dict[TurnStatus, str] = {
    TurnStatus.COMPLETED: "completed",
    TurnStatus.CANCELLED: "cancelled",
    TurnStatus.ERROR: "failed",
}


@dataclass
class Turn:
    """一个完整的用户交互周期（从收到消息到响应完成）。

    Turn 是贯穿整个处理流程的状态容器：
    - execute() 入口设置 turn_id 和可选的确认事件
    - 各状态函数读取上游写入的字段、写入自己的产出给下游
    - 事件从状态转移中产生，reply_id 关联到 turn.turn_id
    """

    # ── 身份（execute 入口创建时设置，不可变）──
    turn_id: str  # 本 Turn 唯一标识，作为 reply_id 关联事件
    inbound: InboundMessage  # 触发本 Turn 的用户消息
    session_id: str  # 所属会话

    # ── 当前状态（状态机读写）──
    status: TurnStatus = TurnStatus.BUILDING

    user_message_id: str = ""  # AgentLoop 进入 Turn 前已持久化的 UserMsg id

    # ── Agent 执行上下文（_build 写入，_run 读取）──
    agent_profile: Any | None = None  # 本轮选定的 Agent Profile 快照值
    config: AgentConfig | None = None  # 本轮实际使用的有效配置快照
    agent: ReActAgent | None = None  # 创建的 Agent 实例，None 表示未进入执行
    messages: list = field(default_factory=list)  # 发给 LLM 的消息列表
    runtime_context: dict = field(default_factory=dict)  # 工具共享的运行时上下文
    final_content: str = ""  # 最后一条完整 assistant 回复（task 工具用）
    retry_count: int = 0
    retry_tokens: set[str] = field(default_factory=set)
    continuation_count: int = 0
    max_continuations: int = 3
    # 每个 Turn 独占一个取消信号；控制型 Agent Hook 只能观察这一个实例。
    cancellation: asyncio.Event = field(default_factory=asyncio.Event)

    # ── 权限确认恢复（/allow、/deny 指令触发时非 None）──
    # 非 None 表示本 Turn 是恢复请求：跳过普通消息构建，
    # 注入历史 context 到新 agent，run() 时传入此事件而非 messages。
    confirm_event: UserConfirmResultEvent | None = None


__all__ = ["PUBLIC_RUN_STATUS", "Turn", "TurnStatus"]
