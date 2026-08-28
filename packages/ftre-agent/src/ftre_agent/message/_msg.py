"""Msg 实体 + append_event 重建引擎（层次 B）。

对齐 AgentScope ``message/_base.py`` 的 Msg + append_event，适配 ftre:
  - pydantic v2 BaseModel（统一技术栈）
  - ToolCallBlock.arguments 是 dict（非 AgentScope 的 str），delta 用内部缓冲
  - error 用 dict 占位（AgentScope ErrorInfo 未引入）
  - 暂不实现人工介入 4 个 case（无权限系统）

append_event 照搬 AgentScope 映射规则，用字符串 match event.type 避免循环 import。
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from ..types import ReplyFinishedReason
from ._block import (
    Base64Source,
    ContentBlock,
    DataBlock,
    HintBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
)

# AgentStreamEvent 仅类型注解用（TYPE_CHECKING），运行时注解字符串化不求值，
# 避免顶层 import event 导致循环（event 依赖 message 的 Block）
if TYPE_CHECKING:
    from ..event import AgentStreamEvent

logger = logging.getLogger(__name__)


def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _to_blocks(content: str | list) -> list:
    """字符串 content 自动包装为单元素 TextBlock 列表；空字符串返回空列表。"""
    if isinstance(content, str):
        return [TextBlock(text=content)] if content else []
    return list(content)


# ══════════════════════════════════════════════════════════════════
# TokenUsage / MsgToken
# ══════════════════════════════════════════════════════════════════

class TokenUsage(BaseModel):
    """单次或累计的 OpenAI-compatible token 用量。"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class MsgToken(BaseModel):
    """assistant Reply 的 token 用量快照。

    - usage: 当前 Reply 内所有 LLM Call 的累计消费
    - last_call_usage: 最后一次成功的 LLM Call 返回的 usage
    """
    usage: TokenUsage
    last_call_usage: TokenUsage


# ══════════════════════════════════════════════════════════════════
# MsgName — Msg 的语义类别
# ══════════════════════════════════════════════════════════════════

class MsgName(StrEnum):
    """Msg 的语义类别。

    ``role`` 说明"谁发的消息"；``name`` 只说明该 Msg 的语义类别，
    不能复用为 agent id 或模型名。

    - DEFAULT：普通用户/助手/系统消息
    - COMPACT：上下文压缩摘要（role=user，正文为完整摘要，是上下文锚点）
    - COMPACT_FAST：快速压缩提示气泡（role=assistant，正文为提示文案，仅供前端
      展示与提醒 Agent 工具输出已被裁剪；**不是**上下文锚点，不参与 tail 计算）
    """
    DEFAULT = "default"
    COMPACT = "compact"
    COMPACT_FAST = "compact_fast"


# ══════════════════════════════════════════════════════════════════
# Msg
# ══════════════════════════════════════════════════════════════════

class Msg(BaseModel):
    """消息实体 —— 事件流的重建目标，AgentScope 协议的"快照视图"。

    一次 reply_stream 产出的所有事件（共享 reply_id）经 append_event 增量重建为本实例。
    """
    model_config = ConfigDict(use_enum_values=True)

    # ── 进 context 的字段 ──
    name: MsgName = MsgName.DEFAULT
    content: list[Annotated[ContentBlock, Field(discriminator="type")]] = Field(default_factory=list)
    role: Literal["user", "assistant", "system"]
    id: str = Field(default_factory=_gen_id)

    # ── 元数据 ──
    metadata: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    token: MsgToken | None = Field(default=None)

    # ── 工作流控制 ──
    finished_at: str | None = Field(default=None)
    finished_reason: ReplyFinishedReason | None = Field(default=None)
    structured_output: dict | None = Field(default=None)
    error: dict[str, Any] | None = Field(default=None)

    # ── 内部缓冲：ToolCall delta 累积（str → END 时 parse 为 dict）──
    _tool_call_input_buf: dict[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _validate_role_content(self) -> Msg:
        """角色约束（对齐 AgentScope）。"""
        for block in self.content:
            if self.role == "user" and block.type not in ("text", "data"):
                raise ValueError("User message can only contain text/data blocks.")
            if self.role == "system" and block.type != "text":
                raise ValueError("System message can only contain text blocks.")
        if self.token is not None and self.role != "assistant":
            raise ValueError(
                f"Msg with role={self.role!r} cannot carry token; "
                "only assistant messages are allowed."
            )
        return self

    # ── 内容访问辅助 ──

    def _find_block(self, block_type: str, block_id: str) -> ContentBlock | None:
        """按 type + id 查找块（对齐 AgentScope）。"""
        for block in self.content:
            if block.type == block_type and block.id == block_id:
                return block
        return None

    def has_content_blocks(self, block_type: str | list[str] | None = None) -> bool:
        if block_type is None:
            return len(self.content) > 0
        typs = [block_type] if isinstance(block_type, str) else block_type
        return any(b.type in typs for b in self.content)

    def get_text_content(self, separator: str = "\n") -> str | None:
        gathered = [b.text for b in self.content if b.type == "text"]
        return separator.join(gathered) if gathered else None

    def get_content_blocks(
        self, block_type: str | list[str] | None = None
    ) -> Sequence[ContentBlock]:
        blocks = self.content or []
        if isinstance(block_type, str):
            return [b for b in blocks if b.type == block_type]
        if isinstance(block_type, list):
            return [b for b in blocks if b.type in block_type]
        return blocks

    # ── 核心：append_event 重建引擎 ──

    def append_event(self, event: AgentStreamEvent) -> Msg:
        """把一个流式事件增量应用到 Msg（对齐 AgentScope append_event）。

        映射规则见 Obsidian「Msg与append_event设计.md」。
        REQUIRE_USER_CONFIRM 会把对应 ToolCall 置 ASKING；
        USER_CONFIRM_RESULT 会持久化为 ALLOWED/FINISHED。其余人工介入事件
        暂不处理，收到时静默跳过。
        """
        # 事件同时携带运行级 reply_id 和消息级 message_id。Steering 会让同一
        # 次运行产生多个 Assistant 消息，因此消息归属优先使用 message_id；
        # 只有旧事件没有该字段时才回退到 reply_id。
        event_message_id = getattr(event, "message_id", None)
        target_id = event_message_id or getattr(event, "reply_id", None)
        if target_id is not None and target_id != self.id:
            logger.warning(
                "Event %s message_id/reply_id %r != msg id %r, skipping.",
                event.__class__.__name__, target_id, self.id,
            )
            return self

        et = event.type

        # ── 生命周期 ──
        if et == "REPLY_END":
            self.finished_at = event.created_at
            self.finished_reason = ReplyFinishedReason(event.finished_reason)
            self.error = event.error

        elif et == "MODEL_CALL_END":
            current = TokenUsage(
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                total_tokens=event.total_tokens,
            )
            if self.token is None:
                self.token = MsgToken(
                    usage=current.model_copy(deep=True),
                    last_call_usage=current.model_copy(deep=True),
                )
            else:
                self.token.usage.prompt_tokens += current.prompt_tokens
                self.token.usage.completion_tokens += current.completion_tokens
                self.token.usage.total_tokens += current.total_tokens
                self.token.last_call_usage = current.model_copy(deep=True)

            # Responses 的原始 Output Item 属于传输元数据，不属于可见内容块。
            # 按模型调用顺序分组保存，MessageContext 在下一次请求时只把当前
            # assistant 片段对应的组带回适配器。
            response_metadata = getattr(event, "response_metadata", None)
            output_items = (
                response_metadata.get("output_items")
                if isinstance(response_metadata, dict)
                else None
            )
            if isinstance(output_items, list) and output_items:
                groups = self.metadata.setdefault("responses_output_item_groups", [])
                if isinstance(groups, list):
                    groups.append([dict(item) for item in output_items if isinstance(item, dict)])

        # ── 文本块三段式 ──
        elif et == "TEXT_BLOCK_START":
            self.content.append(TextBlock(id=event.block_id, text=""))

        elif et == "TEXT_BLOCK_DELTA":
            block = self._find_block("text", event.block_id)
            if block is not None:
                block.text += event.delta
            else:
                logger.warning("TextBlock %r not found, skipping.", event.block_id)

        elif et == "TEXT_BLOCK_END":
            block = self._find_block("text", event.block_id)
            if block is not None:
                block.finished_at = event.created_at

        # ── 思考块三段式 ──
        elif et == "THINKING_BLOCK_START":
            self.content.append(ThinkingBlock(id=event.block_id, thinking=""))

        elif et == "THINKING_BLOCK_DELTA":
            block = self._find_block("thinking", event.block_id)
            if block is not None:
                block.thinking += event.delta
            else:
                logger.warning("ThinkingBlock %r not found, skipping.", event.block_id)

        elif et == "THINKING_BLOCK_END":
            block = self._find_block("thinking", event.block_id)
            if block is not None:
                block.finished_at = event.created_at

        # ── 数据块三段式 ──
        elif et == "DATA_BLOCK_START":
            self.content.append(
                DataBlock(
                    id=event.block_id,
                    source=Base64Source(data="", media_type=event.media_type),
                )
            )

        elif et == "DATA_BLOCK_DELTA":
            block = self._find_block("data", event.block_id)
            if block is None:
                logger.warning("DataBlock %r not found, skipping.", event.block_id)
            elif event.data:
                # 每个 delta 是独立 base64 chunk（自带 padding），必须
                # decode→拼接 bytes→encode，否则字节流损坏
                existing = (
                    base64.b64decode(block.source.data)
                    if block.source.data else b""
                )
                incoming = base64.b64decode(event.data)
                block.source.data = base64.b64encode(
                    existing + incoming
                ).decode("ascii")

        elif et == "DATA_BLOCK_END":
            block = self._find_block("data", event.block_id)
            if block is not None:
                block.finished_at = event.created_at

        # ── 提示块（一次性）──
        elif et == "HINT_BLOCK":
            hint_block = HintBlock(
                id=event.block_id,
                source=event.source,
                hint=event.hint,
            )
            hint_block.finished_at = hint_block.created_at
            self.content.append(hint_block)

        # ── 工具调用三段式（ftre 适配：delta 缓冲 → END parse）──
        elif et == "TOOL_CALL_START":
            self.content.append(
                ToolCallBlock(
                    id=event.tool_call_id,
                    name=event.tool_call_name,
                    arguments={},
                )
            )
            self._tool_call_input_buf[event.tool_call_id] = ""

        elif et == "TOOL_CALL_DELTA":
            # 累积 delta 字符串到缓冲（ftre arguments 是 dict，不能直接 +=）
            buf = self._tool_call_input_buf.get(event.tool_call_id, "")
            self._tool_call_input_buf[event.tool_call_id] = buf + event.delta

        elif et == "TOOL_CALL_END":
            block = self._find_block("tool_call", event.tool_call_id)
            if block is not None:
                # END 携带完整原始参数时，以它作为最终事实。delta 只是实时
                # 展示用的增量；这样即使某些 delta 经 WebSocket 丢失，持久化
                # Msg 仍能得到完整的工具入参。旧事件没有 arguments 时再回退
                # 到本地缓冲，避免改变已有事件重建行为。
                raw = event.arguments or self._tool_call_input_buf.get(
                    event.tool_call_id, ""
                )
                self._tool_call_input_buf.pop(event.tool_call_id, None)
                try:
                    block.arguments = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, TypeError):
                    logger.warning("ToolCall %r args parse failed: %r",
                                   event.tool_call_id, raw)
                    block.arguments = {}
                block.finished_at = event.created_at

        # ── 工具结果三段式 ──
        elif et == "TOOL_RESULT_START":
            self.content.append(
                ToolResultBlock(
                    id=event.tool_call_id,
                    name=event.tool_call_name,
                    output=[],
                    state=ToolResultState.RUNNING,
                )
            )

        elif et == "TOOL_RESULT_TEXT_DELTA":
            block = self._find_block("tool_result", event.tool_call_id)
            if block is None:
                logger.warning("ToolResultBlock %r not found, skipping.",
                               event.tool_call_id)
            else:
                # output 可能是 str（初始）或 list，统一为 list
                if isinstance(block.output, str):
                    block.output = [TextBlock(text=block.output)]
                # 连续 TextDelta 合并到同一个 TextBlock（避免每 delta 一块）
                if not block.output or block.output[-1].type != "text":
                    block.output.append(TextBlock(text=event.delta))
                else:
                    block.output[-1].text += event.delta

        elif et == "TOOL_RESULT_DATA_DELTA":
            block = self._find_block("tool_result", event.tool_call_id)
            if block is None:
                logger.warning("ToolResultBlock %r not found, skipping.",
                               event.tool_call_id)
            else:
                if isinstance(block.output, str):
                    block.output = [TextBlock(text=block.output)]
                src = (
                    Base64Source(data=event.data, media_type=event.media_type)
                    if event.data is not None
                    else __import__("ftre_agent.message._block",
                                    fromlist=["URLSource"]).URLSource(
                        url=str(event.url), media_type=event.media_type)
                )
                block.output.append(DataBlock(id=event.block_id, source=src))

        elif et == "TOOL_RESULT_END":
            block = self._find_block("tool_result", event.tool_call_id)
            if block is not None:
                block.state = event.state
                block.metadata = event.metadata
                block.finished_at = event.created_at
                # 配对 ToolCall 置 FINISHED（保证 SSE 重建与 agent 内部一致）
                call_block = self._find_block("tool_call", event.tool_call_id)
                if call_block is not None:
                    call_block.state = ToolCallState.FINISHED

        # ── 权限确认：把对应 ToolCall 置 ASKING ──
        # RequireUserConfirmEvent 是状态变更信使：它让上游投影出的 Msg 快照里的
        # 目标 tool_call 从 PENDING 变成 ASKING，从而在恢复时保留待确认状态。
        elif et == "REQUIRE_USER_CONFIRM":
            block = self._find_block("tool_call", event.tool_call_id)
            if block is not None:
                block.state = ToolCallState.ASKING
            else:
                logger.warning(
                    "ToolCall %r not found for REQUIRE_USER_CONFIRM, skipping.",
                    event.tool_call_id,
                )

        # ── 权限确认结果：持久化本次用户决定 ──
        elif et == "USER_CONFIRM_RESULT":
            block = self._find_block("tool_call", event.tool_call_id)
            if block is not None:
                block.state = (
                    ToolCallState.ALLOWED
                    if event.approved
                    else ToolCallState.FINISHED
                )
            else:
                logger.warning(
                    "ToolCall %r not found for USER_CONFIRM_RESULT, skipping.",
                    event.tool_call_id,
                )

        # ── 其余人工介入事件暂不处理，静默跳过 ──
        # REQUIRE_EXTERNAL_EXECUTION / EXTERNAL_EXECUTION_RESULT

        return self


# ══════════════════════════════════════════════════════════════════
# 工厂函数（对齐 AgentScope UserMsg/AssistantMsg/SystemMsg）
# ══════════════════════════════════════════════════════════════════

def UserMsg(name: str | MsgName = MsgName.DEFAULT, content: str | list = "", **kwargs) -> Msg:
    """创建 user 消息（content str 自动包 TextBlock）。

    name 默认 MsgName.DEFAULT；压缩摘要场景显式传 MsgName.COMPACT。
    """
    return Msg(name=name, content=_to_blocks(content), role="user", **kwargs)


def AssistantMsg(name: str | MsgName = MsgName.DEFAULT, content: str | list = "", **kwargs) -> Msg:
    """创建 assistant 消息（content str 自动包 TextBlock，默认空）。"""
    return Msg(name=name, content=_to_blocks(content), role="assistant", **kwargs)


def SystemMsg(name: str | MsgName = MsgName.DEFAULT, content: str | list = "", **kwargs) -> Msg:
    """创建 system 消息（content str 自动包 TextBlock）。"""
    return Msg(name=name, content=_to_blocks(content), role="system", **kwargs)
