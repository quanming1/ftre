"""Runtime 调用形状适配测试：只组装 Request，不重复派发流 Hook。"""

import asyncio

import pytest
from ftre_llm import (
    FinishChunk,
    LlmCallConfig,
    LlmCredentials,
    LlmServiceAdapter,
    TextDeltaChunk,
)


class _FakeService:
    async def stream(self, request, *, credentials=None, dispatch_stream_hooks=True):
        assert request.config.model == "model"
        assert credentials.api_key == "key"
        assert dispatch_stream_hooks is False
        yield TextDeltaChunk(index=0, text="ok")
        yield FinishChunk()


@pytest.mark.asyncio
async def test_service_adapter_passes_package_chunks_without_inner_hook():
    adapter = LlmServiceAdapter(
        _FakeService(),
        LlmCallConfig(provider="provider", model="model"),
        LlmCredentials(api_key="key"),
        agent_id="agent",
        session_id="session",
        turn_id="turn",
        cancellation=asyncio.Event(),
    )

    chunks = [
        chunk
        async for chunk in adapter.stream(
            [{"role": "user", "content": "hello"}],
        )
    ]

    assert isinstance(chunks[0], TextDeltaChunk)
    assert isinstance(chunks[-1], FinishChunk)
