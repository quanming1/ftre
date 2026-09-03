"""统一 LLM Service：路由、模型能力、PreparedCall 和单次流调用。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from .contracts import (
    AdapterFactory,
    AdaptersUpdatedPayload,
    AgentRequestPayload,
    LlmAdapter,
    LlmCallConfig,
    LlmCredentials,
    LlmRequest,
    LlmStreamPayload,
    ModelInfo,
    PreparedLlmCall,
    ProviderInfo,
)
from .errors import AdapterNotFoundError, LlmServiceError

logger = logging.getLogger(__name__)


class _Registration:
    """一组路由的可逆注册句柄，替换时保持原子可见。"""

    def __init__(self, service: LlmService, providers: Sequence[str], factory: AdapterFactory) -> None:
        self._service = service
        self.providers = tuple(providers)
        self.factory = factory
        self._disposed = False

    def dispose(self) -> bool:
        if self._disposed:
            return False
        self._disposed = True
        removed: list[str] = []
        for provider in self.providers:
            if self._service._adapters.get(provider) is self.factory:
                self._service._adapters.pop(provider, None)
                removed.append(provider)
        if removed:
            self._service._queue_adapters_updated("dispose", removed)
        return True

    def replace(self, providers: Sequence[str]) -> bool:
        """原子替换路由集合，不产生短暂的空路由窗口。"""

        if self._disposed:
            return False
        target = tuple(dict.fromkeys(providers))
        if not target or any(not isinstance(item, str) or not item.strip() for item in target):
            raise LlmServiceError("providers must contain non-empty strings", "INVALID_ADAPTER")
        conflicts = [
            item
            for item in target
            if item in self._service._adapters and item not in self.providers
        ]
        if conflicts:
            raise LlmServiceError(f"adapter already registered: {conflicts[0]}", "DUPLICATE_ADAPTER")
        for provider in self.providers:
            if self._service._adapters.get(provider) is self.factory:
                self._service._adapters.pop(provider, None)
        for provider in target:
            self._service._adapters[provider] = self.factory
        self.providers = target
        self._service._queue_adapters_updated("replace", target)
        return True


class LlmService:
    """进程内唯一 LLM 调用 Owner；不拥有 Retry、Fallback 或 Session 状态。"""

    key = "llm"

    def __init__(
        self,
        *,
        resolve_credentials: Callable[[str, str], Mapping[str, Any] | None] | None = None,
        hook_dispatch: Callable[..., Any] | None = None,
    ) -> None:
        self._adapters: dict[str, AdapterFactory] = {}
        self._resolve_credentials = resolve_credentials
        self._hook_dispatch = hook_dispatch
        self._prepared_calls: set[PreparedLlmCall] = set()
        self._adapter_notification_tasks: set[asyncio.Task] = set()

    def register_adapter(
        self,
        providers: str | Sequence[str],
        adapter: AdapterFactory,
    ) -> _Registration:
        """原子注册一个或多个 Provider 路由。"""

        names = (providers,) if isinstance(providers, str) else tuple(dict.fromkeys(providers))
        if not names or any(not isinstance(item, str) or not item.strip() for item in names):
            raise LlmServiceError("providers must contain non-empty strings", "INVALID_ADAPTER")
        if any(item in self._adapters for item in names):
            duplicate = next(item for item in names if item in self._adapters)
            raise LlmServiceError(f"adapter already registered: {duplicate}", "DUPLICATE_ADAPTER")
        for provider in names:
            self._adapters[provider] = adapter
        self._queue_adapters_updated("register", names)
        return _Registration(self, names, adapter)

    def list_providers(self) -> tuple[ProviderInfo, ...]:
        return tuple(ProviderInfo(name=name, api_type=name) for name in self._adapters)

    async def resolve_model_info(
        self,
        provider: str,
        model: str,
        *,
        api_type: str | None = None,
        cancellation: asyncio.Event | None = None,
    ) -> ModelInfo:
        """解析精确 Provider/Model 的非敏感能力快照。"""

        if cancellation is not None and cancellation.is_set():
            raise LlmServiceError("LLM request cancelled", "ABORTED")
        values = await self._resolve(provider, model)
        route = api_type or str(values.get("api_type") or provider)
        if route not in self._adapters:
            raise AdapterNotFoundError(route)
        return self._model_info(provider, model, route, values)

    async def prepare_call(
        self,
        config: LlmCallConfig,
        *,
        credentials: LlmCredentials | None = None,
        cancellation: asyncio.Event | None = None,
    ) -> PreparedLlmCall:
        """解析默认配置、捕获当前 Adapter 并返回一次性调用句柄。"""

        if cancellation is not None and cancellation.is_set():
            raise LlmServiceError("LLM request cancelled", "ABORTED")
        values = await self._resolve(config.provider, config.model)
        route = config.api_type or str(values.get("api_type") or config.provider)
        factory = self._adapters.get(route)
        if factory is None:
            raise AdapterNotFoundError(route)
        model_info = self._model_info(config.provider, config.model, route, values)
        effective = replace(config, api_type=route)
        if effective.max_tokens is None and model_info.max_output is not None:
            effective = replace(effective, max_tokens=model_info.max_output)
        if (
            effective.reasoning_effort
            and model_info.reasoning_effort_values
            and effective.reasoning_effort not in model_info.reasoning_effort_values
        ):
            raise LlmServiceError("unsupported reasoning effort", "UNSUPPORTED_REASONING_EFFORT")
        supplied = credentials
        if supplied is None:
            supplied = LlmCredentials(
                api_key=str(values.get("api_key", "")),
                api_base=str(values.get("api_base", "")),
            )
        if not supplied.api_key:
            raise LlmServiceError("LLM credentials are missing", "MISSING_CREDENTIAL")
        try:
            adapter: LlmAdapter = factory(
                model=effective.model,
                api_key=supplied.api_key,
                api_base=supplied.api_base,
                api_type=effective.api_type,
                timeout=effective.timeout,
                max_retries=0,
                max_tokens=effective.max_tokens,
                temperature=effective.temperature,
                reasoning_effort=effective.reasoning_effort or "",
                stop=effective.stop,
            )
        except LlmServiceError:
            raise
        except Exception as exc:
            raise LlmServiceError("failed to create LLM adapter", "INVALID_ADAPTER") from exc
        prepared = PreparedLlmCall(
            effective,
            adapter,
            model_info=model_info,
            retry_policy=adapter.retry_policy() if hasattr(adapter, "retry_policy") else None,
            adapter_defaults=frozenset(values),
            on_complete=self._prepared_calls.discard,
        )
        self._prepared_calls.add(prepared)
        return prepared

    async def stream(
        self,
        request: LlmRequest,
        *,
        credentials: LlmCredentials | None = None,
        dispatch_stream_hooks: bool = True,
    ):
        """执行一次调用。

        直接消费者（Compaction/Title）默认进入 ``llm/stream``；Runtime Runner
        通过 ``LlmServiceAdapter`` 调用时关闭这里的重复派发，由 Runtime 外层
        的同名 Hook 统一拥有 Agent 流包装。
        """

        request = await self._dispatch_request(request)
        prepared = await self.prepare_call(
            request.config,
            credentials=credentials,
            cancellation=request.cancellation,
        )
        def invoke():
            return prepared.stream(request)

        payload = self._stream_payload(request, invoke)
        stream = (
            await self._dispatch_named("llm/stream", payload)
            if dispatch_stream_hooks
            else payload.invoke()
        )
        try:
            async for chunk in stream:
                yield chunk
        finally:
            if hasattr(stream, "aclose"):
                await stream.aclose()

    async def close(self) -> None:
        for task in tuple(self._adapter_notification_tasks):
            task.cancel()
        if self._adapter_notification_tasks:
            await asyncio.gather(*self._adapter_notification_tasks, return_exceptions=True)
        self._adapter_notification_tasks.clear()
        for prepared in tuple(self._prepared_calls):
            prepared.cancel()
        self._prepared_calls.clear()
        self._adapters.clear()

    def _queue_adapters_updated(self, operation: str, providers: Sequence[str]) -> None:
        """异步发布路由变化；注册 API 保持同步且不阻塞 Plugin apply。"""

        if self._hook_dispatch is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 允许脱离事件循环的纯注册单测；Gateway Plugin 始终在 loop 内装配。
            return
        payload = AdaptersUpdatedPayload(
            providers=tuple(dict.fromkeys(providers)),
            operation=operation,
        )
        task = loop.create_task(self._dispatch_adapter_update(payload))
        self._adapter_notification_tasks.add(task)
        task.add_done_callback(self._adapter_notification_tasks.discard)

    async def _dispatch_adapter_update(self, payload: AdaptersUpdatedPayload) -> None:
        try:
            await self._hook_dispatch("llm/adapters-updated", payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 目录通知是观察 Hook，监听器故障不能回滚已经完成的注册事务。
            logger.exception("[llm] adapters-updated listener failed")

    async def _resolve(self, provider: str, model: str) -> Mapping[str, Any]:
        values = self._resolve_credentials(provider, model) if self._resolve_credentials else None
        if inspect.isawaitable(values):
            values = await values
        return values if isinstance(values, Mapping) else {}

    async def _dispatch_request(self, request: LlmRequest) -> LlmRequest:
        if self._hook_dispatch is None:
            return request
        result = await self._hook_dispatch(
            "agent/request",
            AgentRequestPayload(
                agent_id=request.agent_id,
                session_id=request.session_id,
                turn_id=request.turn_id,
                step=int(request.config.metadata.get("step", 0)),
                config=request.config,
                previous_failure=None,
                cancellation=request.cancellation or asyncio.Event(),
            ),
        )
        if isinstance(result, LlmCallConfig):
            return replace(request, config=result)
        return request

    def _stream_payload(self, request: LlmRequest, invoke):
        return LlmStreamPayload(
            agent_id=request.agent_id,
            session_id=request.session_id,
            turn_id=request.turn_id,
            provider=request.config.provider,
            model=request.config.model,
            purpose=request.purpose,
            messages=tuple(message.to_mapping() for message in request.messages),
            tools=tuple(tool.to_mapping() for tool in request.tools),
            cancellation=request.cancellation or asyncio.Event(),
            attempt=request.attempt,
            max_attempts=request.max_attempts,
            invoke=invoke,
        )

    async def _dispatch_named(self, name: str, payload):
        if self._hook_dispatch is None:
            return payload.invoke()
        if name == "llm/stream":
            result = await self._hook_dispatch(name, payload)
            return result
        return payload.invoke()

    @staticmethod
    def _model_info(provider: str, model: str, route: str, values: Mapping[str, Any]) -> ModelInfo:
        return ModelInfo(
            provider=provider,
            model=model,
            api_type=route,
            context_window=_positive_int(values.get("context_window")),
            max_output=_positive_int(values.get("max_output")),
            vision=bool(values.get("vision", False)),
            reasoning_effort_values=tuple(
                item for item in values.get("reasoning_effort_values", ()) if isinstance(item, str)
            ),
        )


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None
