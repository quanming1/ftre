import asyncio
from types import SimpleNamespace

import pytest
from ftre_agent.message import AssistantMsg, MsgName, UserMsg
from ftre_compaction.config import CompactionConfig
from ftre_compaction.service import (
    CompactionService,
    _build_user_messages_section,
    _chunk_messages_by_tokens,
    _merge_chunk_summaries,
    _parse_chunk_sections,
)
from ftre_llm import TextDeltaChunk


class _FakePrepared:
    def __init__(self, config, adapter):
        self.config = config
        self._adapter = adapter

    async def stream(self, request):
        messages = [message.to_mapping() for message in request.messages]
        async for chunk in self._adapter.stream(messages):
            yield chunk


class _FakeLlmService:
    def __init__(self, factory):
        self._factory = factory

    async def prepare_call(self, config, **_kwargs):
        return _FakePrepared(
            config,
            self._factory(
                model=config.model,
                api_type=config.api_type,
                reasoning_effort=config.reasoning_effort,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
            ),
        )

    async def stream(self, request, **kwargs):
        prepared = await self.prepare_call(request.config, **kwargs)
        async for chunk in prepared.stream(request):
            yield chunk


def _config():
    return SimpleNamespace(
        llm=SimpleNamespace(
            provider="summary-provider",
            model="summary-model",
            api_key="key",
            api_base="",
            api_type="completions",
            reasoning_effort="none",
            max_output=4096,
        )
    )


def _record(content: str):
    return UserMsg(content=content).model_dump(mode="json")


def _chunk_summary(label: str) -> str:
    return f"<state_snapshot><current_work>{label}</current_work></state_snapshot>"


def test_user_message_section_is_deterministic_and_skips_compaction_messages():
    records = [
        UserMsg(name=MsgName.DEFAULT, content="第一条").model_dump(mode="json"),
        AssistantMsg(content="助手回复").model_dump(mode="json"),
        UserMsg(
            name=MsgName.COMPACT,
            content="旧摘要里的用户消息不应再次当作原始输入",
            metadata={"hide": True},
        ).model_dump(mode="json"),
        AssistantMsg(
            name=MsgName.COMPACT_FAST,
            content="工具输出已裁剪",
        ).model_dump(mode="json"),
        UserMsg(name=MsgName.DEFAULT, content="第二条").model_dump(mode="json"),
    ]

    section = _build_user_messages_section(records)

    assert "第一条" in section and "第二条" in section
    assert "旧摘要里的用户消息" not in section
    assert "助手回复" not in section
    assert section.index("第一条") < section.index("第二条")


@pytest.mark.asyncio
async def test_user_message_section_is_not_requested_from_llm_and_preserves_previous_summary(monkeypatch):
    prompts: list[str] = []

    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        async def stream(self, messages):
            prompts.append(str(messages[-1]["content"]))
            yield TextDeltaChunk(text=_chunk_summary("摘要"))

    service = CompactionService(
        session_manager=None,
        llm=_FakeLlmService(lambda **kwargs: FakeAdapter(**kwargs)),
    )
    result = await service._run_compact_llm(
        [_record("新增用户消息")],
        config=_config(),
        previous_summary=(
            "<state_snapshot><all_user_messages>用户消息 1: 旧用户消息"
            "</all_user_messages></state_snapshot>"
        ),
        compaction_config=CompactionConfig(chunk_tokens=100),
    )

    assert result is not None
    assert "旧用户消息" in result and "新增用户消息" in result
    assert "<all_user_messages>" not in prompts[0]


def test_chunk_messages_by_tokens_preserves_message_boundaries():
    messages = [_record("甲" * 60), _record("乙" * 60), _record("丙" * 60)]

    chunks = _chunk_messages_by_tokens(messages, 100)

    assert [[item["content"][0]["text"] for item in chunk] for chunk in chunks] == [
        ["甲" * 60],
        ["乙" * 60],
        ["丙" * 60],
    ]


@pytest.mark.asyncio
async def test_each_chunk_calls_one_llm_and_merges_in_order(monkeypatch, caplog):
    prompts: list[str] = []

    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        async def stream(self, messages):
            prompts.append(str(messages[-1]["content"]))
            yield TextDeltaChunk(text=_chunk_summary(f"chunk-{len(prompts)}"))

    service = CompactionService(
        session_manager=None,
        llm=_FakeLlmService(lambda **kwargs: FakeAdapter(**kwargs)),
    )
    caplog.set_level("INFO", logger="ftre_compaction.service")
    result = await service._run_compact_llm(
        [_record("甲" * 60), _record("乙" * 60), _record("丙" * 60)],
        config=_config(),
        compaction_config=CompactionConfig(chunk_tokens=100, chunk_parallelism=3),
    )

    assert len(prompts) == 3
    assert result is not None
    assert result.index("chunk-1") < result.index("chunk-2") < result.index("chunk-3")
    assert sum("chunk=" in record.message and "call tokens=" in record.message for record in caplog.records) == 3
    assert all("model=summary-model" in record.message for record in caplog.records if "call tokens=" in record.message)


@pytest.mark.asyncio
async def test_chunk_parallelism_limits_inflight_llm_calls(monkeypatch):
    active = 0
    peak = 0

    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        async def stream(self, _messages):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            yield TextDeltaChunk(text=_chunk_summary("done"))
            active -= 1

    service = CompactionService(
        session_manager=None,
        llm=_FakeLlmService(lambda **kwargs: FakeAdapter(**kwargs)),
    )
    result = await service._run_compact_llm(
        [_record("消息" * 60) for _ in range(5)],
        config=_config(),
        compaction_config=CompactionConfig(chunk_tokens=100, chunk_parallelism=2),
    )

    assert result is not None
    assert peak == 2


@pytest.mark.asyncio
async def test_only_first_chunk_receives_previous_summary(monkeypatch):
    prompts: list[str] = []

    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        async def stream(self, messages):
            prompts.append(str(messages[-2]["content"]))
            yield TextDeltaChunk(text=_chunk_summary("chunk"))

    service = CompactionService(
        session_manager=None,
        llm=_FakeLlmService(lambda **kwargs: FakeAdapter(**kwargs)),
    )
    await service._run_compact_llm(
        [_record("甲" * 60), _record("乙" * 60)],
        config=_config(),
        previous_summary="旧摘要",
        compaction_config=CompactionConfig(chunk_tokens=100, chunk_parallelism=2),
    )

    assert "旧摘要" in prompts[0]
    assert "旧摘要" not in prompts[1]


@pytest.mark.asyncio
async def test_failed_chunk_retries_only_that_chunk(monkeypatch):
    calls = 0

    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        async def stream(self, _messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                return
            yield TextDeltaChunk(text=_chunk_summary("ok"))

    service = CompactionService(
        session_manager=None,
        llm=_FakeLlmService(lambda **kwargs: FakeAdapter(**kwargs)),
    )
    result = await service._run_compact_llm(
        [_record("甲" * 60), _record("乙" * 60)],
        config=_config(),
        compaction_config=CompactionConfig(
            chunk_tokens=100,
            chunk_parallelism=1,
            chunk_retry_attempts=1,
        ),
    )

    assert result is not None
    assert calls == 3


@pytest.mark.asyncio
async def test_chunk_cancellation_cancels_all_children(monkeypatch):
    started = asyncio.Event()
    cancelled = 0

    async def slow_chunk(*_args, **_kwargs):
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1

    service = CompactionService(session_manager=None)
    monkeypatch.setattr(service, "_run_summary_chunk", slow_chunk)
    task = asyncio.create_task(
        service._run_compact_llm(
            [_record("甲" * 60), _record("乙" * 60)],
            config=_config(),
            compaction_config=CompactionConfig(chunk_tokens=100, chunk_parallelism=2),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == 2


def test_chunk_parser_allows_partial_sections_and_merge_is_deterministic():
    first = _parse_chunk_sections(
        "<state_snapshot><current_work>第一块</current_work></state_snapshot>"
    )
    second = _parse_chunk_sections(
        "<state_snapshot><next_step>第二块</next_step></state_snapshot>"
    )

    assert first is not None and second is not None
    merged = _merge_chunk_summaries([first, second])
    assert merged is not None
    assert merged.index("<current_work>") < merged.index("<next_step>")
    assert "第一块" in merged and "第二块" in merged


def test_chunk_parser_rejects_empty_output():
    assert _parse_chunk_sections("<state_snapshot></state_snapshot>") is None
