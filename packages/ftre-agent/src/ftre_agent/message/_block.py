"""类型化内容块（ContentBlock）—— 对齐 AgentScope 的 pydantic 实现。

对应文档: Obsidian「AgentScope源码分析/消息与事件协议.md」

设计原则（对齐 AgentScope ``message/_block.py``）:
  - pydantic v2 BaseModel（与 AgentScope 一致，统一技术栈）
  - ``type`` 字段用 ``Literal["text"]`` 等字面量，天然作为判别字段
  - 状态机用 ``StrEnum`` + ``ConfigDict(use_enum_values=True)``，
    序列化输出字符串值
  - 字段适配 ftre 现有 OpenAI part 格式：
      * ``ToolCallBlock.arguments`` 用 ``dict``（非 AgentScope 的 str JSON）
      * Block 内部 type 用 snake_case（tool_call/tool_result），转换器
        负责 ↔ ftre 现有的 camelCase（toolCall）
  - 工具结果在 ftre 里是独立 ``{role:"tool"}`` 消息，层次 A 先保留为
    ToolResultBlock（方案 Y：边界转换时再拆回独立消息）

层次 A 边界: 仅定义 Block + 状态机，不引入 Msg / append_event（层次 B）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _gen_id() -> str:
    """生成 16 位 hex block id。"""
    return uuid.uuid4().hex[:16]


def _now_iso() -> str:
    """ISO 8601 时间戳。"""
    return datetime.now(UTC).isoformat()


# ══════════════════════════════════════════════════════════════════
# 工具状态机（对应 AgentScope ToolCallState / ToolResultState）
# ══════════════════════════════════════════════════════════════════

class ToolCallState(StrEnum):
    """工具调用生命周期状态。

    pending   → 初始，未经权限系统处理
    asking     → 等待用户确认
    allowed    → 已批准，待执行
    submitted → 已提交外部执行
    finished  → 完成
    """
    PENDING = "pending"
    ASKING = "asking"
    ALLOWED = "allowed"
    SUBMITTED = "submitted"
    FINISHED = "finished"


class ToolResultState(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    DENIED = "denied"
    RUNNING = "running"


# ══════════════════════════════════════════════════════════════════
# 数据源（DataBlock.source）
# ══════════════════════════════════════════════════════════════════

class Base64Source(BaseModel):
    """base64 编码的二进制数据源。"""
    type: Literal["base64"] = "base64"
    data: str
    media_type: str


class URLSource(BaseModel):
    """URL 指向的二进制数据源。"""
    type: Literal["url"] = "url"
    url: str
    media_type: str


# ══════════════════════════════════════════════════════════════════
# 6 种内容块
# ══════════════════════════════════════════════════════════════════

class TextBlock(BaseModel):
    """纯文本内容块。"""
    type: Literal["text"] = "text"
    text: str
    id: str = Field(default_factory=_gen_id)
    created_at: str = Field(default_factory=_now_iso)
    finished_at: str | None = None


class ThinkingBlock(BaseModel):
    """模型推理过程（思维链）。

    ftre 现有格式里推理内容存在 assistant 消息的 ``reasoning_content`` 字段，
    转换器负责 reasoning_content ↔ ThinkingBlock.thinking 的双向映射。
    """
    type: Literal["thinking"] = "thinking"
    thinking: str
    id: str = Field(default_factory=_gen_id)
    created_at: str = Field(default_factory=_now_iso)
    finished_at: str | None = None


class DataBlock(BaseModel):
    """二进制数据块（图片/音频/视频），source 为 base64 或 URL。"""
    type: Literal["data"] = "data"
    source: Base64Source | URLSource
    name: str | None = None
    id: str = Field(default_factory=_gen_id)
    created_at: str = Field(default_factory=_now_iso)
    finished_at: str | None = None


class HintBlock(BaseModel):
    """提示块：团队消息 / 后台工具结果 / 调度任务触发，传给 LLM 时转 user message。

    ftre 现有 ``UserMessageEvent``（hide=True）语义接近，可视为 hint 的一种。
    ``hint`` 可以是纯文本或多模态内容（TextBlock/DataBlock 的 dict 列表）。
    """
    type: Literal["hint"] = "hint"
    hint: str | list
    source: str | None = None
    id: str = Field(default_factory=_gen_id)
    created_at: str = Field(default_factory=_now_iso)
    finished_at: str | None = Field(default_factory=_now_iso)


class ToolCallBlock(BaseModel):
    """工具调用块。

    与 AgentScope 的差异：``arguments`` 用 ``dict``（贴合 ftre 现有 part 格式），
    而非 AgentScope 的 str JSON。流式增量拼装由上层（层次 C）处理，层次 A
    只承载最终解析后的 dict。

    ``id`` 即 tool_call_id（贴合 AgentScope，ToolCallBlock.id 就是调用 id）。
    """
    model_config = ConfigDict(use_enum_values=True)

    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict = Field(default_factory=dict)
    state: ToolCallState = ToolCallState.PENDING
    created_at: str = Field(default_factory=_now_iso)
    finished_at: str | None = None


class ToolResultBlock(BaseModel):
    """工具执行结果块。

    ftre 现有格式里工具结果是独立 ``{role:"tool", tool_call_id, content}`` 消息，
    层次 A 先统一为块（方案 Y）；边界转换（``to_openai_message``）时再拆回
    独立 tool 消息，保 OpenAI 历史格式兼容。

    ``output`` 可以是纯文本或内容块 dict 列表（多模态结果）。
    """
    model_config = ConfigDict(use_enum_values=True)

    type: Literal["tool_result"] = "tool_result"
    id: str
    name: str
    output: str | list
    state: ToolResultState = ToolResultState.RUNNING
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    finished_at: str | None = None


# ══════════════════════════════════════════════════════════════════
# 类型别名
# ══════════════════════════════════════════════════════════════════

# 所有内容块的联合类型
ContentBlock = TextBlock | ThinkingBlock | DataBlock | HintBlock | ToolCallBlock | ToolResultBlock

# 内容块类型字符串字面量集合
ContentBlockTypes = (
    "text",
    "thinking",
    "data",
    "hint",
    "tool_call",
    "tool_result",
)
