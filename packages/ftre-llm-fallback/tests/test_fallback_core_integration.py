from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from ftre_llm import (
    FinishChunk,
    FinishReason,
    LlmCallConfig,
    LlmFailure,
    LlmStreamPayload,
    TextDeltaChunk,
)
from ftre_llm_fallback.config import parse_config
from ftre_llm_fallback.stream import stream_with_fallback


class _Prepared:
    config = LlmCallConfig(provider="backup", model="backup", api_type="completions")

    async def stream(self, _request):
        yield TextDeltaChunk(index=0, text="backup answer")
        yield FinishChunk(reason=FinishReason(kind="stop"))


class _Service:
    async def prepare_call(self, *_args, **_kwargs):
        return _Prepared()


async def _primary():
    yield FinishChunk(
        reason=FinishReason(
            kind="error",
            failure=LlmFailure(code="timeout", message="primary failed"),
        )
    )


@pytest.mark.asyncio
async def test_last_attempt_fallback_returns_backup_stream():
    payload = LlmStreamPayload(
        agent_id="default",
        session_id="s",
        turn_id="t",
        provider="primary",
        model="primary",
        purpose="conversation",
        messages=({"role": "user", "content": "hello"},),
        tools=(),
        cancellation=asyncio.Event(),
        invoke=lambda: _primary(),
        attempt=1,
        max_attempts=1,
    )
    chunks = [
        chunk
        async for chunk in stream_with_fallback(
            payload,
            _primary(),
            SimpleNamespace(
                resolve_llm=lambda *_: {
                    "model": "backup",
                    "api_type": "completions",
                    "api_key": "k",
                }
            ),
            parse_config({"provider": "backup", "model": "backup", "errors": ["timeout"]}),
            _Service(),
        )
    ]
    assert any(isinstance(chunk, TextDeltaChunk) and chunk.text == "backup answer" for chunk in chunks)
