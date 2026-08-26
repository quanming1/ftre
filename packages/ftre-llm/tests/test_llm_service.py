from __future__ import annotations

import asyncio
import inspect

import pytest
from cordis import Context
from ftre_llm import (
    FinishChunk,
    LlmAdapter,
    LlmCallConfig,
    LlmCredentials,
    LlmRequest,
    LlmService,
    TextDeltaChunk,
)
from ftre_llm.adapters.openai_completions import OpenAICompletionsAdapter
from ftre_llm.adapters.openai_responses import OpenAIResponsesAdapter
from ftre_llm.adapters.plugin import apply as apply_provider_plugin
from ftre_llm.base import OpenAIAdapterBase
from ftre_llm.errors import AdapterNotFoundError, LlmServiceError


class FakeAdapter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def stream(self, request, tools=None):
        del tools
        assert request.messages[0].role == "user"
        yield TextDeltaChunk(index=0, text="ok")
        yield FinishChunk()

    def cancel(self):
        return None


@pytest.mark.asyncio
async def test_registry_and_prepared_call_are_single_owner():
    service = LlmService()
    service.register_adapter("completions", FakeAdapter)
    assert service.list_providers()[0].api_type == "completions"

    config = LlmCallConfig(provider="completions", model="test-model")
    prepared = await service.prepare_call(
        config,
        credentials=LlmCredentials(api_key="test-key"),
    )
    request = LlmRequest.from_parts(config, [{"role": "user", "content": "hello"}])
    chunks = [chunk async for chunk in prepared.stream(request)]
    assert [chunk.type for chunk in chunks] == ["text-delta", "finish"]
    with pytest.raises(RuntimeError, match="only be called once"):
        [chunk async for chunk in prepared.stream(request)]


@pytest.mark.asyncio
async def test_missing_adapter_and_credentials_fail_before_io():
    service = LlmService()
    with pytest.raises(AdapterNotFoundError):
        await service.prepare_call(
            LlmCallConfig(provider="missing", model="m", api_type="completions"),
            credentials=LlmCredentials(api_key="key"),
        )
    service.register_adapter("completions", FakeAdapter)
    with pytest.raises(LlmServiceError) as error:
        await service.prepare_call(LlmCallConfig(provider="p", model="m", api_type="completions"))
    assert error.value.code == "MISSING_CREDENTIAL"


@pytest.mark.asyncio
async def test_close_cancels_prepared_inflight_adapter():
    started = asyncio.Event()
    cancelled = False

    class BlockingAdapter(FakeAdapter):
        async def stream(self, _request, _tools=None):
            nonlocal cancelled
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True
            # 只有在取消状态下才会到达这个分支；yield 使替身保持异步迭代器形状。
            if cancelled:
                yield None

    service = LlmService()
    service.register_adapter("completions", BlockingAdapter)
    config = LlmCallConfig(provider="completions", model="m", api_type="completions")
    prepared = await service.prepare_call(config, credentials=LlmCredentials(api_key="k"))
    request = LlmRequest.from_parts(config, [{"role": "user", "content": "x"}])
    task = asyncio.create_task(_consume(prepared, request))
    await started.wait()
    await service.close()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert cancelled is True


def test_host_protocol_adapters_are_registered_types():
    assert issubclass(OpenAICompletionsAdapter, object)
    assert issubclass(OpenAIResponsesAdapter, object)


def test_adapter_contract_has_one_owner():
    """公开契约和 OpenAI 骨架必须指向同一个 Adapter 类型。"""

    assert OpenAIAdapterBase.__bases__ == (LlmAdapter,)


@pytest.mark.asyncio
async def test_adapter_provider_plugin_owns_registration_lifecycle():
    """具体协议由 Provider Plugin 注册，卸载后路由完整撤销。"""

    context = Context()
    service = LlmService()
    context.provide("llm", service)
    apply_provider_plugin(context)
    assert {item.api_type for item in service.list_providers()} == {"completions", "responses"}
    disposed = context.dispose()
    if inspect.isawaitable(disposed):
        await disposed
    assert service.list_providers() == ()


@pytest.mark.asyncio
async def test_adapter_registration_emits_lifecycle_hook():
    """注册、原子替换和卸载都发布 adapters-updated 观察事实。"""

    events = []

    async def dispatch(name, payload):
        events.append((name, payload.operation, payload.providers))

    service = LlmService(hook_dispatch=dispatch)
    registration = service.register_adapter("completions", FakeAdapter)
    await asyncio.sleep(0)
    registration.replace(("completions", "responses"))
    registration.dispose()
    await asyncio.sleep(0)

    assert events == [
        ("llm/adapters-updated", "register", ("completions",)),
        ("llm/adapters-updated", "replace", ("completions", "responses")),
        ("llm/adapters-updated", "dispose", ("completions", "responses")),
    ]
    await service.close()


async def _consume(prepared, request):
    async for _chunk in prepared.stream(request):
        pass
