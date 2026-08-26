"""BlockAssembler —— StreamChunk 消费侧组装器。

按 index 把交错的 delta 组装为完整 block 序列，并校验协议契约
（PRD-B2 3.3 节）：
- block-start / block-end 按 index 配对，index 从 0 单调递增
- delta 只作用于已 start、未 end 的 block
- usage 在 finish 之前；finish 收尾后无内容
- 畸形序列在 validate() / finish_reason() 处抛 LLMError

react_runner 用它把 chunk 流还原为 Msg ContentBlock 列表；
不做流式转发（流式 UI 事件由消费方在 feed 时自行旁路 yield）。
"""

from __future__ import annotations

import logging

from .errors import LLMError
from .events import (
    BlockEnd,
    BlockStart,
    FinishChunk,
    ReasoningDeltaChunk,
    StreamChunk,
    TextDeltaChunk,
    ToolCallDeltaChunk,
    UsageChunk,
)

logger = logging.getLogger(__name__)

_BLOCK_TYPES = {"text", "reasoning", "tool-call"}


class _OpenBlock:
    """一个已 start、未 end 的 block 的累积态。"""

    __slots__ = ("arguments", "block_type", "call_id", "index", "name", "text")

    def __init__(self, index: int, block_type: str):
        self.index = index
        self.block_type = block_type
        self.text = ""
        self.call_id = ""
        self.name = ""
        self.arguments = ""


class BlockAssembler:
    """消费 StreamChunk 序列，产出完整 block 列表与 finish/usage。

    用法::

        assembler = BlockAssembler()
        async for chunk in adapter.stream(messages, tools):
            assembler.feed(chunk)
            # 旁路转发给 UI（流式 delta）...
        assembler.validate()
        blocks = assembler.blocks()
        finish = assembler.finish_reason()
        usage = assembler.usage()
    """

    def __init__(self) -> None:
        self._open: dict[int, _OpenBlock] = {}
        self._next_index = 0
        # index → 完成的 block（流末按 index 排序输出——block-end 到达顺序
        # 可能与 index 顺序不一致，如 responses 协议 text 块在流末补闭合）
        self._blocks: dict[int, dict] = {}
        self._usage: dict | None = None
        self._finish: FinishChunk | None = None
        self._finished = False
        # block-start 见过但尚未配对的 index 集合（validate 用）
        self._pending: set[int] = set()

    # ── 状态查询 ────────────────────────────────────────────────────────

    @property
    def finished(self) -> bool:
        """是否已收到 finish chunk。"""
        return self._finished

    def blocks(self) -> list[dict]:
        """finish 后的完整 block 序列（按 index 升序——与开块顺序一致，
        不受 block-end 到达顺序影响）。

        max-tokens 截断时丢弃 tool-call 块：截断的 arguments 是不完整
        JSON，无法安全执行（对齐 DSH assembled() 的 max-tokens 过滤）。
        """
        result = [self._blocks[idx] for idx in sorted(self._blocks)]
        if self._finish is not None and self._finish.reason.kind == "max-tokens":
            result = [b for b in result if b.get("type") != "tool-call"]
        return result

    def usage(self) -> dict | None:
        """usage chunk 携带的用量（未收到时为 None）。"""
        return self._usage

    def finish_reason(self) -> FinishChunk | None:
        """finish chunk（未收到时为 None）。"""
        return self._finish

    # ── 喂入 ────────────────────────────────────────────────────────────

    def feed(self, chunk: StreamChunk) -> None:
        """喂入一个 chunk；协议违规立即抛 LLMError（fail-fast）。"""
        if self._finished:
            raise LLMError(
                f"chunk after finish: {getattr(chunk, 'type', type(chunk).__name__)}",
                "STREAM_PROTOCOL",
            )

        if isinstance(chunk, BlockStart):
            self._feed_start(chunk)
        elif isinstance(chunk, TextDeltaChunk):
            self._feed_delta(chunk.index, "text", chunk.text)
        elif isinstance(chunk, ReasoningDeltaChunk):
            self._feed_delta(chunk.index, "reasoning", chunk.text)
        elif isinstance(chunk, ToolCallDeltaChunk):
            self._feed_tool_delta(chunk)
        elif isinstance(chunk, BlockEnd):
            self._feed_end(chunk)
        elif isinstance(chunk, UsageChunk):
            self._usage = chunk.usage
        elif isinstance(chunk, FinishChunk):
            self._finish = chunk
            self._finished = True
        else:
            raise LLMError(
                f"unknown chunk type: {getattr(chunk, 'type', type(chunk).__name__)}",
                "STREAM_PROTOCOL",
            )

    # ── 校验 ────────────────────────────────────────────────────────────

    def validate(self) -> None:
        """流结束后校验契约；违规抛 LLMError。

        - 所有 block-start 均已配对 block-end
        - finish 已收到（未收到视为协议违规）
        """
        if self._pending:
            raise LLMError(
                f"unclosed block(s): {sorted(self._pending)}",
                "STREAM_PROTOCOL",
            )
        if not self._finished:
            raise LLMError("stream ended without finish chunk", "STREAM_CLOSED")

    # ── 内部 ────────────────────────────────────────────────────────────

    def _feed_start(self, chunk: BlockStart) -> None:
        if chunk.index != self._next_index:
            raise LLMError(
                f"block-start index {chunk.index} != expected {self._next_index}",
                "STREAM_PROTOCOL",
            )
        if chunk.block_type not in _BLOCK_TYPES:
            raise LLMError(
                f"unknown block_type {chunk.block_type!r}",
                "STREAM_PROTOCOL",
            )
        self._open[chunk.index] = _OpenBlock(chunk.index, chunk.block_type)
        self._pending.add(chunk.index)
        self._next_index += 1

    def _feed_delta(self, index: int, expected_type: str, text: str) -> None:
        block = self._require_open(index)
        if block.block_type != expected_type:
            raise LLMError(
                f"delta type {expected_type!r} does not match open block "
                f"{block.block_type!r} at index {index}",
                "STREAM_PROTOCOL",
            )
        block.text += text

    def _feed_tool_delta(self, chunk: ToolCallDeltaChunk) -> None:
        block = self._require_open(chunk.index)
        if block.block_type != "tool-call":
            raise LLMError(
                f"tool-call-delta on non tool-call block {block.block_type!r} "
                f"at index {chunk.index}",
                "STREAM_PROTOCOL",
            )
        if chunk.call_id:
            block.call_id = chunk.call_id
        if chunk.name:
            block.name = chunk.name
        block.arguments += chunk.arguments_delta

    def _feed_end(self, chunk: BlockEnd) -> None:
        block = self._require_open(chunk.index)
        # block-end 的 block 必须与累积态一致（以 wire block 为权威，
        # 累积仅用于交叉校验类型）
        wire_block = chunk.block or {}
        if wire_block.get("type") and self._wire_type(block.block_type) != wire_block.get("type"):
            raise LLMError(
                f"block-end type {wire_block.get('type')!r} != open block "
                f"{block.block_type!r} at index {chunk.index}",
                "STREAM_PROTOCOL",
            )
        self._open.pop(chunk.index, None)
        self._pending.discard(chunk.index)
        self._blocks[chunk.index] = self._finalize_block(block)

    def _require_open(self, index: int) -> _OpenBlock:
        block = self._open.get(index)
        if block is None:
            raise LLMError(
                f"delta/end for unopened or closed block index {index}",
                "STREAM_PROTOCOL",
            )
        return block

    @staticmethod
    def _wire_type(block_type: str) -> str:
        # block_type（chunk 词汇）→ Msg ContentBlock type（wire 词汇）
        return {"reasoning": "thinking"}.get(block_type, block_type)

    @staticmethod
    def _finalize_block(block: _OpenBlock) -> dict:
        if block.block_type == "text":
            return {"type": "text", "text": block.text}
        if block.block_type == "reasoning":
            return {"type": "thinking", "thinking": block.text}
        return {
            "type": "tool-call",
            "id": block.call_id,
            "name": block.name,
            "arguments": block.arguments,
        }


