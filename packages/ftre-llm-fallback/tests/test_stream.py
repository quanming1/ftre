from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from ftre_agent_core.llm import (
    FinishChunk,
    FinishReason,
    LLMError,
    LlmFailure,
    TextDeltaChunk,
)
from ftre_llm_fallback.config import parse_config
from ftre_llm_fallback.stream import stream_with_fallback

from ftre.services.llm.hooks import LLMStreamPayload


def _payload(*, attempt=2, max_attempts=2, cancellation=None):
    return LLMStreamPayload(
        agent_id="a",
        session_id="s",
        turn_id="t",
        model="primary",
        messages=({"role": "user", "content": "hi"},),
        tools=(),
        cancellation=cancellation or asyncio.Event(),
        invoke=lambda: (),
        attempt=attempt,
        max_attempts=max_attempts,
    )


def _error(code="timeout", message="failed"):
    return FinishChunk(reason=FinishReason(kind="error", failure=LlmFailure(code=code, message=message)))


async def _collect(iterator):
    return [item async for item in iterator]


@pytest.mark.asyncio
async def test_last_attempt_without_output_uses_backup_once(monkeypatch):
    calls = []

    class Backup:
        async def stream(self, messages, tools=None):
            calls.append((messages, tools))
            yield TextDeltaChunk(text="backup")
            yield FinishChunk(reason=FinishReason(kind="stop"))

        def cancel(self):
            return None

    monkeypatch.setattr("ftre_llm_fallback.stream._create_backup_adapter", lambda _: Backup())
    config_service = SimpleNamespace(resolve_llm=lambda *_: {"model": "backup", "api_type": "completions"})
    result = await _collect(
        stream_with_fallback(
            _payload(),
            _aiter([_error()]),
            config_service,
            parse_config({"provider": "p", "model": "m", "errors": ["timeout"]}),
        )
    )
    assert [item.text for item in result if isinstance(item, TextDeltaChunk)] == ["backup"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_non_last_attempt_never_uses_backup():
    config_service = SimpleNamespace(resolve_llm=lambda *_: {"model": "backup"})
    result = await _collect(
        stream_with_fallback(
            _payload(attempt=1, max_attempts=2),
            _aiter([_error()]),
            config_service,
            parse_config({"provider": "p", "model": "m", "errors": ["timeout"]}),
        )
    )
    assert result[0].reason.kind == "error"


@pytest.mark.asyncio
async def test_partial_primary_output_never_switches():
    config_service = SimpleNamespace(resolve_llm=lambda *_: {"model": "backup"})
    result = await _collect(
        stream_with_fallback(
            _payload(),
            _aiter([TextDeltaChunk(text="partial"), _error()]),
            config_service,
            parse_config({"provider": "p", "model": "m", "errors": ["timeout"]}),
        )
    )
    assert [item.text for item in result if isinstance(item, TextDeltaChunk)] == ["partial"]
    assert result[-1].reason.kind == "error"


@pytest.mark.asyncio
async def test_cancelled_primary_never_switches():
    cancellation = asyncio.Event()
    cancellation.set()
    config_service = SimpleNamespace(resolve_llm=lambda *_: {"model": "backup"})
    result = await _collect(
        stream_with_fallback(
            _payload(cancellation=cancellation),
            _aiter([_error()]),
            config_service,
            parse_config({"provider": "p", "model": "m", "errors": ["timeout"]}),
        )
    )
    assert result[0].reason.kind == "error"


@pytest.mark.asyncio
async def test_overflow_is_never_taken_over():
    calls = 0

    def resolve(*_):
        nonlocal calls
        calls += 1
        return {"model": "backup"}

    result = await _collect(
        stream_with_fallback(
            _payload(),
            _aiter([_error("bad_request", "context length exceeded")]),
            SimpleNamespace(resolve_llm=resolve),
            parse_config({"provider": "p", "model": "m", "errors": ["bad_request"]}),
        )
    )
    assert result[0].reason.kind == "error"
    assert calls == 0


@pytest.mark.asyncio
async def test_backup_failure_returns_original_primary_error(monkeypatch):
    class Backup:
        async def stream(self, messages, tools=None):
            del messages, tools
            yield _error("backup_timeout", "backup failed")

        def cancel(self):
            return None

    monkeypatch.setattr("ftre_llm_fallback.stream._create_backup_adapter", lambda _: Backup())
    result = await _collect(
        stream_with_fallback(
            _payload(),
            _aiter([_error("timeout", "primary failed")]),
            SimpleNamespace(resolve_llm=lambda *_: {"model": "backup"}),
            parse_config({"provider": "p", "model": "m", "errors": ["timeout"]}),
        )
    )
    assert result[-1].reason.failure.code == "timeout"


@pytest.mark.asyncio
async def test_direct_primary_exception_can_fallback_when_no_output(monkeypatch):
    class Backup:
        async def stream(self, messages, tools=None):
            del messages, tools
            yield TextDeltaChunk(text="backup")
            yield FinishChunk(reason=FinishReason(kind="stop"))

        def cancel(self):
            return None

    async def primary():
        raise LLMError(message="rate limited", code="rate_limit")
        yield

    monkeypatch.setattr("ftre_llm_fallback.stream._create_backup_adapter", lambda _: Backup())
    result = await _collect(
        stream_with_fallback(
            _payload(),
            primary(),
            SimpleNamespace(resolve_llm=lambda *_: {"model": "backup"}),
            parse_config({"provider": "p", "model": "m", "errors": ["rate_limit"]}),
        )
    )
    assert [item.text for item in result if isinstance(item, TextDeltaChunk)] == ["backup"]


async def _aiter(items):
    for item in items:
        yield item
