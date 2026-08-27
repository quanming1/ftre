"""StreamChunk —— ftre LLM 流协议（从 Agent Core 迁移的协议 Owner）。

这份协议代码从 ``ftre-agent-core.llm.events`` 迁入，今后由 ftre-llm 维护。
Core 目前仍保留自己的旧副本，直到 Agent Runtime 一并迁出；两套类型不能
混用，所以旧 Core Agent 的过渡适配只允许存在于 Host，不得进入本 Package。

协议契约（适配器必须遵守，BlockAssembler 校验）：

- 每个 block-start 必须有配对的 block-end（同 index），index 从 0 单调递增
- delta 只作用于已 start、未 end 的 block
- usage 必须在 finish 之前；finish 必须是最后一个 chunk，其后无内容
- 适配器可抛异常，但 OpenAIAdapterBase 统一将其转为终止性
  finish {kind: "error"}（取消转为 aborted）——消费方永不面对裸异常

与 ftre Msg ContentBlock 的同构关系（block-end.block）：
  block_type "text"      → TextBlock
  block_type "reasoning" → ThinkingBlock
  block_type "tool-call" → ToolCallBlock
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── finish reason ──────────────────────────────────────────────────────


@dataclass
class LlmFailure:
    """error / aborted finish 携带的失败信息（LLMError 的 wire 形态）。"""

    message: str = ""
    code: str = ""


@dataclass
class FinishReason:
    """一轮 provider 调用的结束原因（DSH 词汇）。

    kind:
      stop        正常完成
      tool-calls  请求执行工具（一轮 tool loop 继续）
      max-tokens  输出达到 max_tokens 截断
      error       provider / 协议错误（携带 failure）
      aborted     调用方取消（携带 failure）
    """

    kind: str = "stop"
    failure: LlmFailure | None = None
    # 原 wire finish_reason 字符串（如 "content_filter"、None 等），
    # 供 tracing / 日志诊断；语义映射以 kind 为准
    raw: str = ""
    # wire 原始响应元数据（response id / model / created 等）
    response_metadata: dict = field(default_factory=dict)


# ── 七种 chunk ─────────────────────────────────────────────────────────


@dataclass
class BlockStart:
    """开启一个内容块；index 从 0 单调递增。"""

    type: str = field(default="block-start", init=False)
    index: int = 0
    block_type: str = "text"  # "text" | "reasoning" | "tool-call"


@dataclass
class TextDeltaChunk:
    """正文文本增量。"""

    type: str = field(default="text-delta", init=False)
    index: int = 0
    text: str = ""


@dataclass
class ReasoningDeltaChunk:
    """推理（thinking）文本增量。"""

    type: str = field(default="reasoning-delta", init=False)
    index: int = 0
    text: str = ""


@dataclass
class ToolCallDeltaChunk:
    """工具调用参数的原始 JSON 片段增量。"""

    type: str = field(default="tool-call-delta", init=False)
    index: int = 0
    call_id: str = ""
    name: str = ""
    arguments_delta: str = ""


@dataclass
class BlockEnd:
    """关闭一个内容块并携带组装完成的完整 block。

    block 为 Msg ContentBlock 同构结构之一：
      {"type": "text", "text": ...}
      {"type": "thinking", "thinking": ...}
      {"type": "tool-call", "id": ..., "name": ..., "arguments": ...}
    """

    type: str = field(default="block-end", init=False)
    index: int = 0
    block: dict = field(default_factory=dict)


@dataclass
class UsageChunk:
    """token 用量（必须在 finish 之前产出）。

    usage 为 OpenAI-compatible 口径的 dict（prompt_tokens / completion_tokens /
    total_tokens 及可选的 *_details）。
    """

    type: str = field(default="usage", init=False)
    usage: dict | None = None


@dataclass
class FinishChunk:
    """一轮 provider 调用结束（必须是最后一个 chunk）。"""

    type: str = field(default="finish", init=False)
    reason: FinishReason = field(default_factory=FinishReason)


# 统一 chunk 类型别名。
StreamChunk = (
    BlockStart
    | TextDeltaChunk
    | ReasoningDeltaChunk
    | ToolCallDeltaChunk
    | BlockEnd
    | UsageChunk
    | FinishChunk
)


# ── 消费侧值对象（非流 chunk）─────────────────────────────────────────


@dataclass
class ToolCall:
    """已组装完整、可执行的工具调用（BlockEnd tool-call 块的消费形态）。

    input 为解析后的参数 dict；None 表示 JSON parse 失败，
    调用方必须按工具错误处理。
    """

    type: str = field(default="tool-call", init=False)
    id: str = ""
    name: str = ""
    input: dict | None = field(default_factory=dict)


__all__ = [
    "BlockEnd",
    "BlockStart",
    "FinishChunk",
    "FinishReason",
    "LlmFailure",
    "ReasoningDeltaChunk",
    "StreamChunk",
    "TextDeltaChunk",
    "ToolCall",
    "ToolCallDeltaChunk",
    "UsageChunk",
]
