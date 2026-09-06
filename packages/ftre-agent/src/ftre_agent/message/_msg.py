"""Msg 实体（层次 B）。

AgentScope ``message/_base.py`` 协议的 ftre 适配：
  - pydantic v2 BaseModel（统一技术栈）
  - ToolCallBlock.arguments 是 dict（非 AgentScope 的 str JSON）
  - error 用 dict 占位（AgentScope ErrorInfo 未引入）

Msg 是 assistant/message 事件的载荷结构，也是读侧
``ftre_agent.session.derive`` 的 fold 输出（事件日志 → 消息列表）。
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..types import ReplyFinishedReason
from ._block import (
    ContentBlock,
    TextBlock,
)

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
    """消息实体——会话事件日志的派生结果与 LLM 上下文载荷。

    assistant/message 事件以 whole-value 方式携带本结构；读侧
    ``derive_messages`` 把事件流 fold 成 Msg 列表（PRD-F43 §1.1）。
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
        """按 type + id 查找块。"""
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

    # 事件→消息的 fold 由 ftre_agent.session.derive（读侧纯函数）承担，
    # Msg 本身只是 assistant/message 事件的载荷结构与派生结果（PRD-F43 §1.1）。


# ══════════════════════════════════════════════════════════════════
# 工厂函数（对齐 AgentScope UserMsg/AssistantMsg/SystemMsg）
# ══════════════════════════════════════════════════════════════════

def UserMsg(name: str | MsgName = MsgName.DEFAULT, content: str | list = "", **kwargs) -> Msg:
    """创建 user 消息（content str 自动包 TextBlock）。"""
    return Msg(name=name, content=_to_blocks(content), role="user", **kwargs)


def AssistantMsg(name: str | MsgName = MsgName.DEFAULT, content: str | list = "", **kwargs) -> Msg:
    """创建 assistant 消息（content str 自动包 TextBlock，默认空）。"""
    return Msg(name=name, content=_to_blocks(content), role="assistant", **kwargs)


def SystemMsg(name: str | MsgName = MsgName.DEFAULT, content: str | list = "", **kwargs) -> Msg:
    """创建 system 消息（content str 自动包 TextBlock）。"""
    return Msg(name=name, content=_to_blocks(content), role="system", **kwargs)
