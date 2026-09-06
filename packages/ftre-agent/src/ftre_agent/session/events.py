"""会话事件表——wire 协议契约的唯一实现（PRD-F41 §4.1/§4.2）。

13 种事件 = 表面 5（user/message, assistant/message, tool/result, hint/message,
compact/message）+ 流式 4（assistant/chunk, tool/call-start, tool/result-start,
approval/asked）+ 生命周期 4（turn/start, turn/retry, turn/end, session/status）。

信封：{type, seq, time, message_id?, data}；seq 由 SessionLog 分配（严格连续），
time 为 epoch 毫秒。surface 事件与 chunk 事件携带 message_id；生命周期事件缺省。

本模块是纯契约层：无 IO、无 Host 依赖；服务端（derive_messages）与客户端
（ConversationAssembler golden 对拍）共用同一张表。
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ── surface/ignorable 注册表（F41 FR3/FR8）─────────────────────────
# 只有 surface 事件构成"消息表面"（模型可见历史与消息列表 fold 输入）。
SURFACE_EVENT_TYPES: frozenset[str] = frozenset({
    "user/message", "assistant/message", "tool/result",
    "hint/message", "compact/message",
})
# 未知事件类型的默认策略：拒绝重建（load 时抛错）。标 ignorable 才允许跳过。
IGNORABLE_EVENT_TYPES: frozenset[str] = frozenset()

ALL_EVENT_TYPES: frozenset[str] = frozenset({
    *SURFACE_EVENT_TYPES,
    "assistant/chunk", "tool/call-start", "tool/result-start", "approval/asked",
    "turn/start", "turn/retry", "turn/end", "session/status",
})


class _EventBase(BaseModel):
    """事件信封公共字段。seq/time 由 SessionLog.append 赋值。"""
    model_config = ConfigDict(extra="forbid")
    seq: int = -1
    time: int = 0
    message_id: str | None = None


# ── data payloads ─────────────────────────────────────────────────

class UserPart(BaseModel):
    """user/message content 的 part 形状（开放契约）。

    ``type`` 判别（text/skill/image/image_file…），其余字段按 type 语义开放
    （extra=allow）；既是运行时文档，也是 desktop wire 类型的生成源。
    """
    model_config = ConfigDict(extra="allow")
    type: str
    text: str | None = None
    data: Any = None
    path: str | None = None
    mime_type: str | None = None


class UserMessageData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: list[UserPart] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""


class AssistantMessageData(BaseModel):
    """whole-value：data.message 是完整 Msg.model_dump(mode="json")。"""
    model_config = ConfigDict(extra="forbid")
    message: dict[str, Any]


class ToolResultData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_call_id: str
    name: str
    output: list[Any] = Field(default_factory=list)
    state: str  # success | error | interrupted | denied
    metadata: dict[str, Any] = Field(default_factory=dict)


class HintData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hint: str | list[Any]
    source: str | None = None


class CompactData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = "summary"  # summary | fast
    summary_text: str = ""
    through_message_id: str = ""
    trigger: str = "auto"
    tokens_before: int = 0
    tokens_after: int = 0
    tool_results: int = 0  # fast 模式裁剪的工具结果数
    # fast 模式实际裁剪的 ToolResultBlock.id；新日志按 id 精确 fold，
    # 没有该字段的旧日志才退化为按 compact 事件之前的数量裁剪。
    tool_result_ids: list[str] = Field(default_factory=list)


class AssistantChunkData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str  # text | thinking | tool_input | tool_result_text
    delta: str = ""
    block_id: str | None = None
    tool_call_id: str | None = None


class ToolCallStartData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultStartData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_call_id: str
    name: str


class ApprovalAskedData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    rule_id: str | None = None


class TurnStartData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str
    request_id: str = ""
    trigger: str = "user"  # user | command | confirm | cron | plugin | system
    command_name: str = ""
    agent_id: str = "default"
    model: str = ""


class TurnRetryData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str
    code: str
    message: str
    attempt: int
    max_attempts: int


class TurnEndData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str
    request_id: str = ""
    outcome: str  # completed | error | cancelled | paused
    reason: str = ""
    error: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    iterations: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionStatusData(BaseModel):
    """仅 blocked 进入/退出会写入日志（运行态由 turn 事件推导）。"""
    model_config = ConfigDict(extra="forbid")
    status: str = "blocked"
    reason: str = ""


# ── 事件模型（信封 + data）────────────────────────────────────────

class UserMessage(_EventBase):
    type: Literal["user/message"] = "user/message"
    data: UserMessageData


class AssistantMessage(_EventBase):
    type: Literal["assistant/message"] = "assistant/message"
    data: AssistantMessageData


class ToolResult(_EventBase):
    type: Literal["tool/result"] = "tool/result"
    data: ToolResultData


class HintMessage(_EventBase):
    type: Literal["hint/message"] = "hint/message"
    data: HintData


class CompactMessage(_EventBase):
    type: Literal["compact/message"] = "compact/message"
    data: CompactData


class AssistantChunk(_EventBase):
    type: Literal["assistant/chunk"] = "assistant/chunk"
    data: AssistantChunkData


class ToolCallStart(_EventBase):
    type: Literal["tool/call-start"] = "tool/call-start"
    data: ToolCallStartData


class ToolResultStart(_EventBase):
    type: Literal["tool/result-start"] = "tool/result-start"
    data: ToolResultStartData


class ApprovalAsked(_EventBase):
    type: Literal["approval/asked"] = "approval/asked"
    data: ApprovalAskedData


class TurnStart(_EventBase):
    type: Literal["turn/start"] = "turn/start"
    data: TurnStartData


class TurnRetry(_EventBase):
    type: Literal["turn/retry"] = "turn/retry"
    data: TurnRetryData


class TurnEnd(_EventBase):
    type: Literal["turn/end"] = "turn/end"
    data: TurnEndData


class SessionStatus(_EventBase):
    type: Literal["session/status"] = "session/status"
    data: SessionStatusData


SessionEvent = Annotated[
    UserMessage | AssistantMessage | ToolResult | HintMessage | CompactMessage | AssistantChunk | ToolCallStart | ToolResultStart | ApprovalAsked | TurnStart | TurnRetry | TurnEnd | SessionStatus,
    Field(discriminator="type"),
]

# Runtime 产出的事件模型集合（类型注解用；含未提交语义，seq 由 SessionLog 赋值）
RuntimeEvent = (
    UserMessage | AssistantMessage | ToolResult | HintMessage | CompactMessage
    | AssistantChunk | ToolCallStart | ToolResultStart | ApprovalAsked
    | TurnStart | TurnRetry | TurnEnd | SessionStatus
)

__all__ = [
    "ALL_EVENT_TYPES",
    "IGNORABLE_EVENT_TYPES",
    "SURFACE_EVENT_TYPES",
    "ApprovalAsked",
    "ApprovalAskedData",
    "AssistantChunk",
    "AssistantChunkData",
    "AssistantMessage",
    "AssistantMessageData",
    "CompactData",
    "CompactMessage",
    "HintData",
    "HintMessage",
    "SessionEvent",
    "SessionStatus",
    "SessionStatusData",
    "ToolCallStart",
    "ToolCallStartData",
    "ToolResult",
    "ToolResultData",
    "ToolResultStart",
    "ToolResultStartData",
    "TurnEnd",
    "TurnEndData",
    "TurnRetry",
    "TurnRetryData",
    "TurnStart",
    "TurnStartData",
    "UserMessage",
    "UserMessageData",
    "UserPart",
]
