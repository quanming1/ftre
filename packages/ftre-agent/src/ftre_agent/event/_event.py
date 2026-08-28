"""扁平 Pydantic AgentStreamEvent 定义。

所有事件继承 EventBase，使用 ``model_dump(mode="json")`` 扁平序列化。
RetryEvent 是 ftre 的扩展事件。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..message import (
    DataBlock,
    TextBlock,
    ToolResultState,
)
from ..types import ReplyFinishedReason


def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class EventType(StrEnum):
    """事件类型枚举（AgentScope 对齐）。"""
    REPLY_START = "REPLY_START"
    REPLY_END = "REPLY_END"
    MODEL_CALL_START = "MODEL_CALL_START"
    MODEL_CALL_END = "MODEL_CALL_END"
    TEXT_BLOCK_START = "TEXT_BLOCK_START"
    TEXT_BLOCK_DELTA = "TEXT_BLOCK_DELTA"
    TEXT_BLOCK_END = "TEXT_BLOCK_END"
    DATA_BLOCK_START = "DATA_BLOCK_START"
    DATA_BLOCK_DELTA = "DATA_BLOCK_DELTA"
    DATA_BLOCK_END = "DATA_BLOCK_END"
    THINKING_BLOCK_START = "THINKING_BLOCK_START"
    THINKING_BLOCK_DELTA = "THINKING_BLOCK_DELTA"
    THINKING_BLOCK_END = "THINKING_BLOCK_END"
    HINT_BLOCK = "HINT_BLOCK"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_DELTA = "TOOL_CALL_DELTA"
    TOOL_CALL_END = "TOOL_CALL_END"
    TOOL_RESULT_START = "TOOL_RESULT_START"
    TOOL_RESULT_TEXT_DELTA = "TOOL_RESULT_TEXT_DELTA"
    TOOL_RESULT_DATA_DELTA = "TOOL_RESULT_DATA_DELTA"
    TOOL_RESULT_END = "TOOL_RESULT_END"
    EXCEED_MAX_ITERS = "EXCEED_MAX_ITERS"
    REQUIRE_USER_CONFIRM = "REQUIRE_USER_CONFIRM"
    USER_CONFIRM_RESULT = "USER_CONFIRM_RESULT"
    RETRY = "retry"
    CUSTOM = "CUSTOM"
    USER_MESSAGE = "USER_MESSAGE"


# ══════════════════════════════════════════════════════════════════
# EventBase
# ══════════════════════════════════════════════════════════════════

class EventBase(BaseModel):
    """事件基类（pydantic，model_dump 扁平序列化）。"""
    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(default_factory=_gen_id)
    created_at: str = Field(default_factory=_now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # reply_id 代表整次 Agent Reply；message_id 代表本次事件所属的具体
    # AssistantMsg。两者分开后，同一轮中插入 UserMessage 不需要复制或重命名
    # 已有消息。非 Assistant 事件可以保持 None。
    message_id: str | None = None


# ── 生命周期 ──
class ReplyStartEvent(EventBase):
    type: Literal["REPLY_START"] = "REPLY_START"
    session_id: str
    reply_id: str
    name: str
    role: Literal["user", "assistant", "system"] = "assistant"


class ReplyEndEvent(EventBase):
    type: Literal["REPLY_END"] = "REPLY_END"
    session_id: str
    reply_id: str
    finished_reason: ReplyFinishedReason = ReplyFinishedReason.COMPLETED
    error: dict[str, Any] | None = None


class ExceedMaxItersEvent(EventBase):
    type: Literal["EXCEED_MAX_ITERS"] = "EXCEED_MAX_ITERS"
    reply_id: str
    name: str


# ── 模型调用 ──
class ModelCallStartEvent(EventBase):
    type: Literal["MODEL_CALL_START"] = "MODEL_CALL_START"
    reply_id: str
    model_name: str


class ModelCallEndEvent(EventBase):
    type: Literal["MODEL_CALL_END"] = "MODEL_CALL_END"
    reply_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    finished_reason: str = "completed"
    # Responses 适配器返回的原始 Output Item 元数据。它只用于 Host
    # 持久化和下一轮协议重放，不参与客户端可见文本渲染。
    response_metadata: dict[str, Any] = Field(default_factory=dict)


# ── 文本块流式 ──
class TextBlockStartEvent(EventBase):
    type: Literal["TEXT_BLOCK_START"] = "TEXT_BLOCK_START"
    reply_id: str
    block_id: str

class TextBlockDeltaEvent(EventBase):
    type: Literal["TEXT_BLOCK_DELTA"] = "TEXT_BLOCK_DELTA"
    reply_id: str
    block_id: str
    delta: str

class TextBlockEndEvent(EventBase):
    type: Literal["TEXT_BLOCK_END"] = "TEXT_BLOCK_END"
    reply_id: str
    block_id: str


# ── 数据块流式 ──
class DataBlockStartEvent(EventBase):
    type: Literal["DATA_BLOCK_START"] = "DATA_BLOCK_START"
    reply_id: str
    block_id: str
    media_type: str

class DataBlockDeltaEvent(EventBase):
    type: Literal["DATA_BLOCK_DELTA"] = "DATA_BLOCK_DELTA"
    reply_id: str
    block_id: str
    data: str
    media_type: str

class DataBlockEndEvent(EventBase):
    type: Literal["DATA_BLOCK_END"] = "DATA_BLOCK_END"
    reply_id: str
    block_id: str


# ── 思考块流式 ──
class ThinkingBlockStartEvent(EventBase):
    type: Literal["THINKING_BLOCK_START"] = "THINKING_BLOCK_START"
    reply_id: str
    block_id: str

class ThinkingBlockDeltaEvent(EventBase):
    type: Literal["THINKING_BLOCK_DELTA"] = "THINKING_BLOCK_DELTA"
    reply_id: str
    block_id: str
    delta: str

class ThinkingBlockEndEvent(EventBase):
    type: Literal["THINKING_BLOCK_END"] = "THINKING_BLOCK_END"
    reply_id: str
    block_id: str


# ── 提示块（一次性）──
class HintBlockEvent(EventBase):
    type: Literal["HINT_BLOCK"] = "HINT_BLOCK"
    reply_id: str
    block_id: str
    source: str | None = None
    hint: str | list[TextBlock | DataBlock]


# ── 工具调用流式 ──
class ToolCallStartEvent(EventBase):
    type: Literal["TOOL_CALL_START"] = "TOOL_CALL_START"
    reply_id: str
    tool_call_id: str
    tool_call_name: str

class ToolCallDeltaEvent(EventBase):
    type: Literal["TOOL_CALL_DELTA"] = "TOOL_CALL_DELTA"
    reply_id: str
    tool_call_id: str
    delta: str

class ToolCallEndEvent(EventBase):
    type: Literal["TOOL_CALL_END"] = "TOOL_CALL_END"
    reply_id: str
    tool_call_id: str
    # 完整的原始 JSON 参数。增量事件用于实时展示，结束事件是客户端恢复和
    # 持久化对齐的最终事实；保留字符串可覆盖丢失/乱序的 delta，也不丢失
    # Provider 返回的原始 JSON 形态。
    arguments: str = ""


# ── 工具结果流式 ──
class ToolResultStartEvent(EventBase):
    type: Literal["TOOL_RESULT_START"] = "TOOL_RESULT_START"
    reply_id: str
    tool_call_id: str
    tool_call_name: str

class ToolResultTextDeltaEvent(EventBase):
    type: Literal["TOOL_RESULT_TEXT_DELTA"] = "TOOL_RESULT_TEXT_DELTA"
    reply_id: str
    tool_call_id: str
    delta: str

class ToolResultDataDeltaEvent(EventBase):
    type: Literal["TOOL_RESULT_DATA_DELTA"] = "TOOL_RESULT_DATA_DELTA"
    reply_id: str
    tool_call_id: str
    block_id: str = Field(default_factory=_gen_id)
    media_type: str
    data: str | None = None
    url: str | None = None

class ToolResultEndEvent(EventBase):
    type: Literal["TOOL_RESULT_END"] = "TOOL_RESULT_END"
    reply_id: str
    tool_call_id: str
    state: ToolResultState
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── 权限确认（HITL）──
class RequireUserConfirmEvent(EventBase):
    """通知上层：某个工具调用命中 ASK，正在等待用户确认。

    由 Core 在权限决策为 ASK 时产出（PermissionEngine 只返回决策，不产事件）。
    产出前对应 ToolCallBlock.state 应已置为 ASKING 并写回 AgentState.context。
    产出本事件不结束回复，不产 ReplyEndEvent；等待用户回传确认后用同一 reply_id 继续。
    """
    type: Literal["REQUIRE_USER_CONFIRM"] = "REQUIRE_USER_CONFIRM"
    reply_id: str
    tool_call_id: str
    tool_call_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    rule_id: str | None = None


class UserConfirmResultEvent(EventBase):
    """用户对某个待确认工具调用的决定（输入事件，不属于 AgentStreamEvent）。

    由上层在收到 RequireUserConfirmEvent 后回传，作为 run() 的输入驱动恢复：
      - approved=True  → 该工具调用从 ASKING 转 ALLOWED，恢复后执行
      - approved=False → 产生 DENIED 工具结果，不执行

    ``reply_id`` 与 ``tool_call_id`` 必须与挂起时一致，否则视为非法输入被拒绝。
    """
    type: Literal["USER_CONFIRM_RESULT"] = "USER_CONFIRM_RESULT"
    reply_id: str
    tool_call_id: str
    approved: bool


# ── RetryEvent（ftre 特有，继承 EventBase）──
class RetryEvent(EventBase):
    """重试事件。ftre 特有（AgentScope 无），对齐 EventBase 风格。"""
    type: Literal["retry"] = "retry"
    reply_id: str
    code: str
    message: str
    attempt: int
    max_attempts: int


# ── 自定义扩展 ──
class CustomEvent(EventBase):
    type: Literal["CUSTOM"] = "CUSTOM"
    name: str
    value: dict = Field(default_factory=dict)


class UserMessageEvent(EventBase):
    """一条完整用户输入对应的 Event。

    ``content`` 与 ``message_metadata`` 用于宿主投影为 UserMsg；``data`` 是宿主
    可选的实时 echo 数据。投影字段不重复进入线缆 payload。
    """

    type: Literal["USER_MESSAGE"] = "USER_MESSAGE"
    reply_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    content: list[Any] = Field(default_factory=list, exclude=True)
    message_metadata: dict[str, Any] = Field(default_factory=dict, exclude=True)


# ── 联合类型 ──
type AgentStreamEvent = ReplyStartEvent | ReplyEndEvent | ExceedMaxItersEvent | ModelCallStartEvent | ModelCallEndEvent | TextBlockStartEvent | TextBlockDeltaEvent | TextBlockEndEvent | DataBlockStartEvent | DataBlockDeltaEvent | DataBlockEndEvent | ThinkingBlockStartEvent | ThinkingBlockDeltaEvent | ThinkingBlockEndEvent | HintBlockEvent | ToolCallStartEvent | ToolCallDeltaEvent | ToolCallEndEvent | ToolResultStartEvent | ToolResultTextDeltaEvent | ToolResultDataDeltaEvent | ToolResultEndEvent | RequireUserConfirmEvent | RetryEvent | CustomEvent | UserMessageEvent
