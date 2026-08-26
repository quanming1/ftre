"""适配器单元测试（PRD-B2 AC4）：fake openai 流事件 → StreamChunk 断言。

completions / responses 两个适配器分别喂 fake 流，验证：
- chunk 序列遵守协议契约（配对 / usage→finish 顺序 / index 单调）
- block 内容正确（文本聚合、reasoning 聚合、tool-call arguments 聚合 + call_id）
- 畸形 finish_reason（None / 未知值）映射 error finish
- provider 异常收敛为终止 error finish（消费方不面对裸异常）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from ftre_llm import (
    BlockAssembler,
    FinishChunk,
    UsageChunk,
)
from ftre_llm.adapters.openai_completions import OpenAICompletionsAdapter
from ftre_llm.adapters.openai_responses import OpenAIResponsesAdapter

# ── fake openai 流对象 ─────────────────────────────────────────────────


@dataclass
class _FakeFunction:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _FakeToolCallDelta:
    index: int | None = None
    id: str | None = None
    function: _FakeFunction | None = None


@dataclass
class _FakeDelta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list | None = None


@dataclass
class _FakeChoice:
    delta: _FakeDelta | None = None
    finish_reason: str | None = None


@dataclass
class _FakeChunk:
    choices: list = field(default_factory=list)
    usage: Any = None
    id: str | None = None
    model: str | None = None


class _FakeCompletionsStream:
    """async 迭代 fake chunk 序列；可注入异常。"""

    def __init__(self, chunks, raise_exc: Exception | None = None):
        self._chunks = chunks
        self._raise = raise_exc
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._raise is not None:
            exc, self._raise = self._raise, None
            raise exc
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


@dataclass
class _FakeUsageObj:
    """model_dump 形态的 usage（openai SDK 对象）。"""
    prompt_tokens: int = 10
    completion_tokens: int = 5
    total_tokens: int = 15

    def model_dump(self, exclude_none: bool = True):
        del exclude_none
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class _FakeResponsesUsageObj:
    """Responses API 标准字段形态的 SDK usage 对象。"""

    input_tokens: int = 10
    output_tokens: int = 5
    total_tokens: int = 15

    def model_dump(self, exclude_none: bool = True):
        del exclude_none
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


def _make_completions_adapter(chunks, raise_exc=None):
    adapter = OpenAICompletionsAdapter(model="test-model", api_key="k")
    stream = _FakeCompletionsStream(list(chunks), raise_exc)
    async def _create(**params):
        return stream
    adapter._client.chat.completions.create = _create  # type: ignore[attr-defined]
    return adapter, stream


async def _collect(agen):
    return [c async for c in agen]


# ── completions 适配器 ─────────────────────────────────────────────────


class TestCompletionsAdapter:
    @pytest.mark.asyncio
    async def test_text_reasoning_toolcall_chunks(self):
        chunks = [
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(reasoning_content="思考"))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(reasoning_content="…"))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content="Hello"))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=" world"))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(tool_calls=[
                _FakeToolCallDelta(index=0, id="call-1", function=_FakeFunction(name="bash")),
            ]))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(tool_calls=[
                _FakeToolCallDelta(index=0, function=_FakeFunction(arguments='{"command":')),
            ]))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(tool_calls=[
                _FakeToolCallDelta(index=0, function=_FakeFunction(arguments=' "ls"}')),
            ]))]),
            _FakeChunk(choices=[_FakeChoice(finish_reason="tool_calls")]),
            _FakeChunk(usage=_FakeUsageObj(), choices=[]),
        ]
        adapter, stream = _make_completions_adapter(chunks)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))

        # 契约：以 finish 收尾
        assert isinstance(out[-1], FinishChunk)
        assert out[-1].reason.kind == "tool-calls"
        # usage 在 finish 前
        assert isinstance(out[-2], UsageChunk)
        assert out[-2].usage["total_tokens"] == 15

        # 组装器吃整个序列应通过契约校验
        asm = BlockAssembler()
        for c in out:
            asm.feed(c)
        asm.validate()
        blocks = asm.blocks()
        assert blocks == [
            {"type": "thinking", "thinking": "思考…"},
            {"type": "text", "text": "Hello world"},
            {"type": "tool-call", "id": "call-1", "name": "bash", "arguments": '{"command": "ls"}'},
        ]
        assert stream.closed

    @pytest.mark.asyncio
    async def test_tool_arguments_buffer_until_call_id_arrives(self):
        """参数先于 call_id 到达时，首段 JSON 也必须被补发。"""
        chunks = [
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(tool_calls=[
                _FakeToolCallDelta(index=0, function=_FakeFunction(arguments='{"command":')),
            ]))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(tool_calls=[
                _FakeToolCallDelta(index=0, id="call-late", function=_FakeFunction(name="bash")),
            ]))]),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(tool_calls=[
                _FakeToolCallDelta(index=0, function=_FakeFunction(arguments=' "ls"}')),
            ]))]),
            _FakeChunk(choices=[_FakeChoice(finish_reason="tool_calls")]),
        ]
        adapter, _ = _make_completions_adapter(chunks)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))

        deltas = [chunk for chunk in out if getattr(chunk, "type", "") == "tool-call-delta"]
        assert [chunk.arguments_delta for chunk in deltas] == [
            '{"command":',
            ' "ls"}',
        ]
        assert all(chunk.call_id == "call-late" for chunk in deltas)

    @pytest.mark.asyncio
    async def test_stop_finish(self):
        chunks = [
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content="hi"))]),
            _FakeChunk(choices=[_FakeChoice(finish_reason="stop")]),
        ]
        adapter, _ = _make_completions_adapter(chunks)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        assert isinstance(out[-1], FinishChunk)
        assert out[-1].reason.kind == "stop"

    @pytest.mark.asyncio
    async def test_length_finish_maps_max_tokens(self):
        chunks = [
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content="xx"))]),
            _FakeChunk(choices=[_FakeChoice(finish_reason="length")]),
        ]
        adapter, _ = _make_completions_adapter(chunks)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        assert out[-1].reason.kind == "max-tokens"

    @pytest.mark.asyncio
    async def test_null_finish_reason_with_output_maps_stop(self):
        """Muse 实测：finish_reason 恒 null。有内容产出时宽容映射 stop（不浪费产出）。"""
        chunks = [
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content="hi"))]),
            _FakeChunk(choices=[_FakeChoice(finish_reason=None)]),
        ]
        adapter, _ = _make_completions_adapter(chunks)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        finish = out[-1]
        assert isinstance(finish, FinishChunk)
        assert finish.reason.kind == "stop"

    @pytest.mark.asyncio
    async def test_null_finish_reason_without_output_maps_error(self):
        """无任何内容产出 + finish_reason null → error finish（真畸形）。"""
        chunks = [
            _FakeChunk(choices=[_FakeChoice(finish_reason=None)]),
        ]
        adapter, _ = _make_completions_adapter(chunks)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        finish = out[-1]
        assert isinstance(finish, FinishChunk)
        assert finish.reason.kind == "error"
        assert finish.reason.failure is not None
        assert finish.reason.failure.code == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_unknown_finish_reason_without_output_maps_error(self):
        chunks = [
            _FakeChunk(choices=[_FakeChoice(finish_reason="content_filter")]),
        ]
        adapter, _ = _make_completions_adapter(chunks)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        assert out[-1].reason.kind == "error"
        assert out[-1].reason.failure.code == "CONTENT_FILTER"

    @pytest.mark.asyncio
    async def test_provider_exception_becomes_error_finish(self):
        """消费方永不面对裸异常：provider 异常 → 终止 error finish。"""
        chunks = [_FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content="x"))])]
        exc = RuntimeError("boom")
        adapter, _ = _make_completions_adapter(chunks, raise_exc=exc)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        finish = out[-1]
        assert isinstance(finish, FinishChunk)
        assert finish.reason.kind == "error"
        assert "boom" in finish.reason.failure.message

    @pytest.mark.asyncio
    async def test_reasoning_only_response(self):
        """纯 reasoning（无正文）也能组装。"""
        chunks = [
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(reasoning_content="内部思考"))]),
            _FakeChunk(choices=[_FakeChoice(finish_reason="stop")]),
        ]
        adapter, _ = _make_completions_adapter(chunks)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        asm = BlockAssembler()
        for c in out:
            asm.feed(c)
        asm.validate()
        assert asm.blocks() == [{"type": "thinking", "thinking": "内部思考"}]


# ── responses 适配器 ───────────────────────────────────────────────────


@dataclass
class _FakeRespItem:
    id: str = "item-1"
    type: str = "message"
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None


@dataclass
class _FakeRespEvent:
    type_name: str
    item: Any = None
    item_id: str | None = None
    delta: str | None = None
    response: Any = None


class _FakeResponsesStream:
    def __init__(self, events):
        self._events = events
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        ev = self._events.pop(0)
        # 动态替换类名为事件名，让适配器的 type(event).__name__ 分发生效
        new_cls = type(ev.type_name, (type(ev),), {"__name__": ev.type_name})
        object.__setattr__(ev, "__class__", new_cls)
        return ev

    def close(self):
        self.closed = True


@dataclass
class _FakeResponse:
    usage: Any = None
    status: str = "completed"
    id: str = "resp-1"
    model: str = "test-model"
    created_at: int = 1
    incomplete_details: Any = None


def _make_responses_adapter(events):
    adapter = OpenAIResponsesAdapter(model="test-model", api_key="k")
    stream = _FakeResponsesStream(list(events))
    async def _create(**params):
        return stream
    adapter._client.responses.create = _create  # type: ignore[attr-defined]
    return adapter, stream


class TestResponsesAdapter:
    @pytest.mark.asyncio
    async def test_standard_responses_usage_maps_to_core_fields(self):
        """Responses 的 input/output token 字段应满足 Runner 的统一契约。"""
        events = [
            _FakeRespEvent(
                "ResponseCompletedEvent",
                response=_FakeResponse(usage=_FakeResponsesUsageObj()),
            ),
        ]
        adapter, _ = _make_responses_adapter(events)

        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))

        usage = next(chunk.usage for chunk in out if isinstance(chunk, UsageChunk))
        assert usage == {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }

    @pytest.mark.asyncio
    async def test_text_toolcall_chunks(self):
        events = [
            _FakeRespEvent("ResponseTextDeltaEvent", item_id="msg-1", delta="Hi"),
            _FakeRespEvent("ResponseTextDeltaEvent", item_id="msg-1", delta=" there"),
            _FakeRespEvent("ResponseOutputItemAddedEvent", item=_FakeRespItem(
                id="fc-1", type="function_call", call_id="call-9", name="read")),
            _FakeRespEvent("ResponseFunctionCallArgumentsDeltaEvent", item_id="fc-1", delta='{"path":'),
            _FakeRespEvent("ResponseFunctionCallArgumentsDeltaEvent", item_id="fc-1", delta=' "a.py"}'),
            _FakeRespEvent("ResponseOutputItemDoneEvent", item=_FakeRespItem(
                id="fc-1", type="function_call", call_id="call-9", name="read",
                arguments='{"path": "a.py"}')),
            _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse(usage=_FakeUsageObj())),
        ]
        adapter, stream = _make_responses_adapter(events)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))

        asm = BlockAssembler()
        for c in out:
            asm.feed(c)
        asm.validate()

        assert asm.blocks() == [
            {"type": "text", "text": "Hi there"},
            {"type": "tool-call", "id": "call-9", "name": "read", "arguments": '{"path": "a.py"}'},
        ]
        assert asm.finish_reason().reason.kind == "tool-calls"
        assert asm.usage()["total_tokens"] == 15
        assert stream.closed

    @pytest.mark.asyncio
    async def test_reasoning_delta_events(self):
        events = [
            _FakeRespEvent("ResponseReasoningDeltaEvent", item_id="rs-1", delta="think "),
            _FakeRespEvent("ResponseReasoningDeltaEvent", item_id="rs-1", delta="deep"),
            _FakeRespEvent("ResponseTextDeltaEvent", item_id="msg-1", delta="answer"),
            _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse()),
        ]
        adapter, _ = _make_responses_adapter(events)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        asm = BlockAssembler()
        for c in out:
            asm.feed(c)
        asm.validate()
        assert asm.blocks() == [
            {"type": "thinking", "thinking": "think deep"},
            {"type": "text", "text": "answer"},
        ]
        assert asm.finish_reason().reason.kind == "stop"

    @pytest.mark.asyncio
    async def test_real_responses_reasoning_text_delta_events(self):
        """OpenAI SDK 的真实 Responses 事件名必须产出 thinking block。

        OpenCode 直连的 curl/SSE 事件是 ``response.reasoning_text.delta``，
        SDK 将其解析为 ``ResponseReasoningTextDeltaEvent``；此前适配器只识别
        旧的 ``ResponseReasoningDeltaEvent``，会让前端只看到最终文本。
        """
        events = [
            _FakeRespEvent("ResponseReasoningTextDeltaEvent", item_id="rs-1", delta="think "),
            _FakeRespEvent("ResponseReasoningTextDeltaEvent", item_id="rs-1", delta="deep"),
            _FakeRespEvent("ResponseTextDeltaEvent", item_id="msg-1", delta="answer"),
            _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse()),
        ]
        adapter, _ = _make_responses_adapter(events)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        asm = BlockAssembler()
        for chunk in out:
            asm.feed(chunk)
        asm.validate()
        assert asm.blocks() == [
            {"type": "thinking", "thinking": "think deep"},
            {"type": "text", "text": "answer"},
        ]

    @pytest.mark.asyncio
    async def test_incomplete_maps_max_tokens(self):
        @dataclass
        class _Incomplete:
            reason: str = "max_output_tokens"

        events = [
            _FakeRespEvent("ResponseTextDeltaEvent", item_id="msg-1", delta="xx"),
            _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse(
                incomplete_details=_Incomplete(), status="incomplete")),
        ]
        adapter, _ = _make_responses_adapter(events)
        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        assert out[-1].reason.kind == "max-tokens"

    @pytest.mark.asyncio
    async def test_provider_exception_becomes_error_finish(self):
        class _ExplodingStream(_FakeResponsesStream):
            async def __anext__(self):
                raise RuntimeError("net down")

        adapter = OpenAIResponsesAdapter(model="t", api_key="k")
        exploding = _ExplodingStream([])
        async def _create(**params):
            return exploding
        adapter._client.responses.create = _create  # type: ignore[attr-defined]

        out = await _collect(adapter.stream([{"role": "user", "content": "hi"}]))
        finish = out[-1]
        assert isinstance(finish, FinishChunk)
        assert finish.reason.kind == "error"
        assert "net down" in finish.reason.failure.message

    @pytest.mark.asyncio
    async def test_user_content_normalized_to_input_text(self):
        """chat 风格 [{"type": "text"}] user content → input_text（Muse 严格校验）。"""
        captured = {}

        adapter = OpenAIResponsesAdapter(model="t", api_key="k")

        async def _create(**kwargs):
            captured.update(kwargs)
            return _FakeResponsesStream([
                _FakeRespEvent("ResponseTextDeltaEvent", item_id="m1", delta="ok"),
                _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse()),
            ])

        adapter._client.responses.create = _create  # type: ignore[attr-defined]
        messages = [
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ]},
        ]
        [c async for c in adapter.stream(messages)]

        first_input = captured["input"][0]
        assert first_input == {"role": "user", "content": [
            {"type": "input_text", "text": "hello"},
            {"type": "input_image", "image_url": "data:image/png;base64,x"},
        ]}
        assert captured["instructions"] == "be nice"

    @pytest.mark.asyncio
    async def test_user_content_string_passthrough(self):
        """字符串 user content 原生支持，直接透传不包数组。"""
        captured = {}

        adapter = OpenAIResponsesAdapter(model="t", api_key="k")

        async def _create(**kwargs):
            captured.update(kwargs)
            return _FakeResponsesStream([
                _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse()),
            ])

        adapter._client.responses.create = _create  # type: ignore[attr-defined]
        [c async for c in adapter.stream([{"role": "user", "content": "plain hi"}])]

        assert captured["input"][0]["content"] == "plain hi"

    @pytest.mark.asyncio
    async def test_user_text_only_array_flattened_to_string(self):
        """纯文本 user 数组 → 扁平化字符串（对齐 DSH pi-ai context.ts：
        content.every(text) ? join('') : 数组；chat 词汇数组被严格实现 400）。"""
        captured = {}

        adapter = OpenAIResponsesAdapter(model="t", api_key="k")

        async def _create(**kwargs):
            captured.update(kwargs)
            return _FakeResponsesStream([
                _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse()),
            ])

        adapter._client.responses.create = _create  # type: ignore[attr-defined]
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]}]
        [c async for c in adapter.stream(messages)]

        assert captured["input"][0]["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_react_history_assistant_content_flattened(self):
        """ReAct 多轮历史：assistant chat 数组 content → 字符串。

        回归测试（Muse / gpt-5.6-terra 实测 400 根因）：Responses API 的
        assistant content 只接受字符串或 output_text 数组，to_openai_message
        产出的 [{"type": "text"}] 数组必须扁平化，否则第二轮带 assistant
        历史即触发 400 input[N].content。
        """
        captured = {}

        adapter = OpenAIResponsesAdapter(model="t", api_key="k")

        async def _create(**kwargs):
            captured.update(kwargs)
            return _FakeResponsesStream([
                _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse()),
            ])

        adapter._client.responses.create = _create  # type: ignore[attr-defined]
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "list files"}]},
            # assistant：chat 数组 content + tool_calls（to_openai_message 形态）
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "我来查看。"}],
                "reasoning_content": "thinking...",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "a.py\nb.py"},
            # 无 tool_calls 的 assistant 历史同样要扁平化
            {"role": "assistant", "content": [{"type": "text", "text": "done."}]},
            {"role": "user", "content": "thanks"},
        ]
        [c async for c in adapter.stream(messages)]

        input_items = captured["input"]
        assert input_items == [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": "我来查看。"},
            {"type": "function_call", "call_id": "call-1", "name": "bash",
             "arguments": '{"command": "ls"}'},
            {"type": "function_call_output", "call_id": "call-1", "output": "a.py\nb.py"},
            {"role": "assistant", "content": "done."},
            {"role": "user", "content": "thanks"},
        ]
        assert captured["instructions"] == "sys"

    @pytest.mark.asyncio
    async def test_thinking_tool_history_replays_reasoning_item(self):
        """Thinking 模式的下一轮必须回传上一轮 reasoning_text。

        OpenCode DeepSeek V4 Flash 在 Tool Result 后继续推理时会校验该 item；
        只回传 assistant/function_call 会返回 400。
        """
        captured = {}
        adapter = OpenAIResponsesAdapter(
            model="deepseek-v4-flash",
            api_key="k",
            reasoning_effort="high",
        )

        async def _create(**kwargs):
            captured.update(kwargs)
            return _FakeResponsesStream([
                _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse()),
            ])

        adapter._client.responses.create = _create  # type: ignore[attr-defined]
        messages = [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "先检查文件，再执行命令。",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"git status"}',
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "clean"},
        ]

        [chunk async for chunk in adapter.stream(messages)]

        reasoning = captured["input"][1]
        assert reasoning["type"] == "reasoning"
        assert reasoning["id"].startswith("rs_")
        assert reasoning["summary"] == []
        assert "status" not in reasoning
        assert reasoning["content"] == [{
            "type": "reasoning_text",
            "text": "先检查文件，再执行命令。",
        }]
        assert captured["input"][2:] == [
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "bash",
                "arguments": '{"command":"git status"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "clean",
            },
        ]

    @pytest.mark.asyncio
    async def test_gpt_responses_omits_legacy_reasoning_content(self):
        """GPT Responses 不发送旧会话的 reasoning content 数组。"""
        captured = {}
        adapter = OpenAIResponsesAdapter(
            model="gpt-5.6-luna",
            api_key="k",
            reasoning_effort="max",
        )

        async def _create(**kwargs):
            captured.update(kwargs)
            return _FakeResponsesStream([
                _FakeRespEvent("ResponseCompletedEvent", response=_FakeResponse()),
            ])

        adapter._client.responses.create = _create  # type: ignore[attr-defined]
        messages = [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "旧会话思考",
            },
        ]

        [chunk async for chunk in adapter.stream(messages)]

        assert captured["input"] == [{"role": "user", "content": "inspect"}]
