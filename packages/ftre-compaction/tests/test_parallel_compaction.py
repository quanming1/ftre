import asyncio
from types import SimpleNamespace

import pytest
from ftre_agent_core.llm.events import TextDeltaChunk
from ftre_agent_core.message import UserMsg
from ftre_compaction.config import CompactionConfig
from ftre_compaction.service import (
    _SUMMARY_WORKERS,
    CompactionService,
    _merge_summary_parts,
    _parse_worker_sections,
)


def _config():
    return SimpleNamespace(
        llm=SimpleNamespace(
            model="summary-model",
            api_key="key",
            api_base="",
            api_type="completions",
            reasoning_effort="none",
            max_output=4096,
        )
    )


def _record(message):
    return message.model_dump(mode="json")


def _worker_name(prompt: str) -> str:
    for spec in _SUMMARY_WORKERS:
        if f"<{spec.sections[0]}>...</{spec.sections[0]}>" in prompt:
            return spec.name
    raise AssertionError(f"unknown worker prompt: {prompt}")


@pytest.mark.asyncio
async def test_parallel_workers_share_snapshot_and_overlap(monkeypatch):
    active = 0
    peak = 0
    prompts: list[str] = []

    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        async def stream(self, messages):
            nonlocal active, peak
            prompt = str(messages[-1]["content"])
            prompts.append(prompt)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            spec = next(item for item in _SUMMARY_WORKERS if item.name == _worker_name(prompt))
            yield TextDeltaChunk(
                text="\n".join(
                    f"<{section}>{spec.name}-{section}</{section}>"
                    for section in spec.sections
                )
            )
            active -= 1

    monkeypatch.setattr(
        "ftre_compaction.service.create_llm_handler",
        lambda _api_type, **kwargs: FakeAdapter(**kwargs),
    )
    service = CompactionService(session_manager=None)
    result = await service._run_compact_llm(
        [_record(UserMsg(content="请保留这个请求"))],
        config=_config(),
        compaction_config=CompactionConfig(parallel_workers=3),
    )

    assert peak == 3
    assert len(prompts) == 3
    assert result is not None
    assert result.index("<primary_request_and_intent>") < result.index(
        "<key_technical_concepts>"
    ) < result.index("<problem_solving>")


@pytest.mark.asyncio
async def test_parallel_worker_retries_only_failed_part(monkeypatch):
    calls: dict[str, int] = {}

    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        async def stream(self, messages):
            prompt = str(messages[-1]["content"])
            name = _worker_name(prompt)
            calls[name] = calls.get(name, 0) + 1
            if name == "intent" and calls[name] == 1:
                return
            spec = next(item for item in _SUMMARY_WORKERS if item.name == name)
            yield TextDeltaChunk(
                text="\n".join(
                    f"<{section}>{name}</{section}>" for section in spec.sections
                )
            )

    monkeypatch.setattr(
        "ftre_compaction.service.create_llm_handler",
        lambda _api_type, **kwargs: FakeAdapter(**kwargs),
    )
    service = CompactionService(session_manager=None)
    result = await service._run_compact_llm(
        [_record(UserMsg(content="需要重试的摘要"))],
        config=_config(),
        compaction_config=CompactionConfig(parallel_retry_attempts=1),
    )

    assert result is not None
    assert calls == {"intent": 2, "technical": 1, "continuity": 1}


@pytest.mark.asyncio
async def test_parallel_worker_cancellation_propagates_to_all_parts(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    cancelled = 0

    async def slow_worker(*_args, **_kwargs):
        nonlocal active, cancelled
        active += 1
        if active == len(_SUMMARY_WORKERS):
            started.set()
        try:
            await release.wait()
            return {}
        finally:
            cancelled += 1

    service = CompactionService(session_manager=None)
    monkeypatch.setattr(service, "_run_summary_worker", slow_worker)
    task = asyncio.create_task(
        service._run_compact_llm(
            [_record(UserMsg(content="需要取消的摘要"))],
            config=_config(),
            compaction_config=CompactionConfig(parallel_workers=3),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == len(_SUMMARY_WORKERS)


def test_merge_rejects_missing_worker_section():
    parts = {
        spec.name: {section: "内容" for section in spec.sections}
        for spec in _SUMMARY_WORKERS
    }
    parts["technical"].pop("errors_and_fixes")
    assert _merge_summary_parts(parts) is None


def test_worker_parser_accepts_wrapped_xml_and_strips_analysis():
    spec = _SUMMARY_WORKERS[0]
    raw = (
        "<analysis>内部草稿</analysis>"
        "<state_snapshot>"
        "<primary_request_and_intent>保留登录目标</primary_request_and_intent>"
        "<all_user_messages>用户要求中文</all_user_messages>"
        "</state_snapshot>"
    )
    assert _parse_worker_sections(raw, spec) == {
        "primary_request_and_intent": "保留登录目标",
        "all_user_messages": "用户要求中文",
    }
