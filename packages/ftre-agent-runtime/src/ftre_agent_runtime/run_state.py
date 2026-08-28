"""Runner 运行状态、动作模型与执行器间数据载体。

本模块定义 ReAct 状态机的全部"词汇表"：
  - RunStatus / RunState / CancelledError：运行生命周期
  - Reasoning / Acting / Exit：三种动作类型
  - TurnResult / ExitOutcome：执行器间传递的数据
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ftre_agent.types import ReplyFinishedReason
from ftre_llm import LLMError, ToolCall
from pydantic import BaseModel

if TYPE_CHECKING:
    from ftre_agent.tracing import TraceSpan


# ═══════════════════════════════════════════════════════════════
# 运行状态
# ═══════════════════════════════════════════════════════════════

class RunStatus(str, Enum):
    """单次 run() 调用的生命周期状态。"""
    IDLE = "idle"
    RUNNING = "running"
    # PAUSED：因权限 ASK 挂起，等待用户确认。非终态——收到确认后可恢复继续。
    # 不产 ReplyEnd、不 finalize，reply_id 保留在 RunState 供恢复复用。
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class CancelledError(Exception):
    """内部取消异常，与 asyncio.CancelledError 区分。"""


@dataclass
class RunState:
    """一次 run() 执行期间的可变状态。"""
    # 生命周期
    status: RunStatus = RunStatus.IDLE
    iteration: int = 0
    done_reason: ReplyFinishedReason | None = None
    error: str | None = None
    error_code: str | None = None

    # Tracing
    trace_span: TraceSpan | None = None

    # 运行上下文
    runtime_context: dict = field(default_factory=dict)
    reply_id: str = ""
    # 当前 AssistantMsg 的唯一 id。一个 run 可以在 before-reasoning 注入正式
    # UserMessage 后旋转到新的 message_id，但 reply_id 保持整次运行不变。
    message_id: str = ""
    turn_id: str = ""

    # 空响应恢复（仅 _decide 读写）
    empty_retries: int = 0
    in_finalization: bool = False

    # token 统计
    token_usage: dict = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    })

    @property
    def is_cancelled(self) -> bool:
        return self.status == RunStatus.CANCELLED

    @property
    def is_done(self) -> bool:
        """是否处于终态。"""
        return self.status in (RunStatus.COMPLETED, RunStatus.ERROR, RunStatus.CANCELLED)

    def start(self) -> None:
        """重置全部字段，开始新一轮执行。"""
        self.status = RunStatus.RUNNING
        self.iteration = 0
        self.error = None
        self.error_code = None
        self.done_reason = None
        self.empty_retries = 0
        self.in_finalization = False
        self.trace_span = None
        self.reply_id = ""
        self.message_id = ""
        self.turn_id = self.runtime_context.get("turn_id") or f"turn_{uuid.uuid4().hex[:12]}"
        self.token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }


# ═══════════════════════════════════════════════════════════════
# 动作类型（纯数据，由 _decide 返回，由执行器消费）
# ═══════════════════════════════════════════════════════════════

class Reasoning(BaseModel):
    """下一步：调用大模型进行推理。"""
    hint: str | None = None
    tool_choice: str | None = None
    force_no_tools: bool = False


class Acting(BaseModel):
    """下一步：执行模型产生的工具调用。"""
    tool_calls: list[ToolCall]


class Exit(BaseModel):
    """下一步：结束（或暂停）当前回复。"""
    finished_reason: ReplyFinishedReason
    exit_msg: Any | None = None
    error: str | None = None
    error_code: str | None = None


# ═══════════════════════════════════════════════════════════════
# 执行器间数据载体
# ═══════════════════════════════════════════════════════════════

@dataclass
class TurnResult:
    """一轮 LLM 推理的结构化产物，供 _decide() 消费。"""
    text: str
    reasoning: str
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: dict | None = None
    error: LLMError | None = None


@dataclass
class ExitOutcome:
    """Exit 执行结果，可能让主循环继续而非退出。"""
    should_continue: bool = False
    continue_hint: str | None = None
