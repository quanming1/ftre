"""StreamChunk 协议契约测试（PRD-B2 AC1）。

合法序列被 BlockAssembler 正确组装；畸形序列被拒：
- 缺 block-end（validate 报 unclosed）
- finish 后余 chunk（feed 报 chunk after finish）
- index 跳跃（block-start index != next_index）
- delta 作用于未开启的 block
- delta 类型与 block 类型不匹配
- 流结束无 finish（validate 报 STREAM_CLOSED）
"""

from __future__ import annotations

import pytest
from ftre_llm.block_assembler import BlockAssembler
from ftre_llm.errors import LLMError
from ftre_llm.events import (
    BlockEnd,
    BlockStart,
    FinishChunk,
    FinishReason,
    ReasoningDeltaChunk,
    TextDeltaChunk,
    ToolCallDeltaChunk,
    UsageChunk,
)


def _ok_sequence():
    """合法序列：reasoning + text + tool-call 交错，usage 后 finish。"""
    return [
        BlockStart(index=0, block_type="reasoning"),
        ReasoningDeltaChunk(index=0, text="thinking "),
        ReasoningDeltaChunk(index=0, text="hard"),
        BlockEnd(index=0, block={"type": "thinking", "thinking": "thinking hard"}),
        BlockStart(index=1, block_type="text"),
        TextDeltaChunk(index=1, text="Hello "),
        TextDeltaChunk(index=1, text="world"),
        BlockEnd(index=1, block={"type": "text", "text": "Hello world"}),
        BlockStart(index=2, block_type="tool-call"),
        ToolCallDeltaChunk(index=2, call_id="c1", name="bash", arguments_delta='{"command":'),
        ToolCallDeltaChunk(index=2, call_id="c1", name="bash", arguments_delta=' "ls"}'),
        BlockEnd(
            index=2,
            block={
                "type": "tool-call",
                "id": "c1",
                "name": "bash",
                "arguments": '{"command": "ls"}',
            },
        ),
        UsageChunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        FinishChunk(reason=FinishReason(kind="tool-calls")),
    ]


class TestValidSequence:
    def test_assembles_blocks_in_order(self):
        asm = BlockAssembler()
        for c in _ok_sequence():
            asm.feed(c)
        asm.validate()

        blocks = asm.blocks()
        assert len(blocks) == 3
        assert blocks[0] == {"type": "thinking", "thinking": "thinking hard"}
        assert blocks[1] == {"type": "text", "text": "Hello world"}
        assert blocks[2] == {
            "type": "tool-call",
            "id": "c1",
            "name": "bash",
            "arguments": '{"command": "ls"}',
        }

    def test_usage_and_finish_exposed(self):
        asm = BlockAssembler()
        for c in _ok_sequence():
            asm.feed(c)
        asm.validate()

        assert asm.usage() == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        assert asm.finish_reason().reason.kind == "tool-calls"
        assert asm.finished is True

    def test_empty_stream_only_finish(self):
        asm = BlockAssembler()
        asm.feed(UsageChunk(usage=None))
        asm.feed(FinishChunk(reason=FinishReason(kind="stop")))
        asm.validate()
        assert asm.blocks() == []
        assert asm.finish_reason().reason.kind == "stop"

    def test_max_tokens_finish_drops_tool_call_blocks(self):
        """max-tokens 截断时 tool-call 的 arguments 不完整，不可安全执行，
        blocks() 输出时丢弃（对齐 DSH assembled()）。"""
        asm = BlockAssembler()
        asm.feed(BlockStart(index=0, block_type="text"))
        asm.feed(TextDeltaChunk(index=0, text="partial"))
        asm.feed(BlockEnd(index=0, block={"type": "text", "text": "partial"}))
        asm.feed(BlockStart(index=1, block_type="tool-call"))
        asm.feed(
            ToolCallDeltaChunk(index=1, call_id="c1", name="bash", arguments_delta='{"command":')
        )
        asm.feed(
            BlockEnd(
                index=1,
                block={
                    "type": "tool-call",
                    "id": "c1",
                    "name": "bash",
                    "arguments": '{"command":',
                },
            )
        )
        asm.feed(FinishChunk(reason=FinishReason(kind="max-tokens")))
        asm.validate()

        assert asm.blocks() == [{"type": "text", "text": "partial"}]


class TestMalformedSequence:
    def test_missing_block_end_rejected(self):
        asm = BlockAssembler()
        asm.feed(BlockStart(index=0, block_type="text"))
        asm.feed(TextDeltaChunk(index=0, text="hi"))
        # 不 feed BlockEnd，直接 finish
        asm.feed(FinishChunk(reason=FinishReason(kind="stop")))
        with pytest.raises(LLMError, match="unclosed"):
            asm.validate()

    def test_chunk_after_finish_rejected(self):
        asm = BlockAssembler()
        asm.feed(FinishChunk(reason=FinishReason(kind="stop")))
        with pytest.raises(LLMError, match="after finish"):
            asm.feed(TextDeltaChunk(index=0, text="late"))

    def test_index_jump_rejected(self):
        asm = BlockAssembler()
        asm.feed(BlockStart(index=0, block_type="text"))
        asm.feed(BlockEnd(index=0, block={"type": "text", "text": "a"}))
        with pytest.raises(LLMError, match="index 2 != expected 1"):
            asm.feed(BlockStart(index=2, block_type="text"))

    def test_first_index_must_be_zero(self):
        asm = BlockAssembler()
        with pytest.raises(LLMError, match="index 1 != expected 0"):
            asm.feed(BlockStart(index=1, block_type="text"))

    def test_delta_on_unopened_block_rejected(self):
        asm = BlockAssembler()
        with pytest.raises(LLMError, match="unopened or closed"):
            asm.feed(TextDeltaChunk(index=0, text="hi"))

    def test_delta_type_mismatch_rejected(self):
        asm = BlockAssembler()
        asm.feed(BlockStart(index=0, block_type="text"))
        with pytest.raises(LLMError, match="does not match"):
            asm.feed(ReasoningDeltaChunk(index=0, text="hm"))

    def test_tool_delta_on_text_block_rejected(self):
        asm = BlockAssembler()
        asm.feed(BlockStart(index=0, block_type="text"))
        with pytest.raises(LLMError, match="non tool-call block"):
            asm.feed(ToolCallDeltaChunk(index=0, call_id="c", name="n", arguments_delta="{}"))

    def test_block_end_type_mismatch_rejected(self):
        asm = BlockAssembler()
        asm.feed(BlockStart(index=0, block_type="text"))
        with pytest.raises(LLMError, match="block-end type"):
            asm.feed(BlockEnd(index=0, block={"type": "thinking", "thinking": "x"}))

    def test_missing_finish_rejected(self):
        asm = BlockAssembler()
        asm.feed(BlockStart(index=0, block_type="text"))
        asm.feed(BlockEnd(index=0, block={"type": "text", "text": "a"}))
        with pytest.raises(LLMError, match="without finish"):
            asm.validate()

    def test_unknown_block_type_rejected(self):
        asm = BlockAssembler()
        with pytest.raises(LLMError, match="unknown block_type"):
            asm.feed(BlockStart(index=0, block_type="image"))

    def test_double_end_rejected(self):
        asm = BlockAssembler()
        asm.feed(BlockStart(index=0, block_type="text"))
        asm.feed(BlockEnd(index=0, block={"type": "text", "text": "a"}))
        with pytest.raises(LLMError, match="unopened or closed"):
            asm.feed(BlockEnd(index=0, block={"type": "text", "text": "a"}))


