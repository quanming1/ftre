"""把一次 Core 风格调用接到 ``LlmService``，不转换流事件。

Agent Runner 的最小调用形状是 ``stream(messages, tools)``，而 LlmService 需要
完整的 ``LlmRequest``。这个适配器只负责补齐配置、凭据和运行坐标；Service
产出的 ``ftre_llm.events.StreamChunk`` 原样返回给调用方。

它不 import ``ftre-agent-core``，因此 LLM Package 仍可独立安装。Core 退役时，
只要 Agent Runtime 不再需要 ``stream(messages, tools)`` 这个入口，本文件也可
删除，而不会影响 Provider、Compaction 或 Title。
"""

from __future__ import annotations

import asyncio

from .contracts import LlmCallConfig, LlmCredentials, LlmRequest
from .service import LlmService


class LlmServiceAdapter:
    """将 Core Runner 的调用参数组装为一次公开 LlmService 请求。"""

    def __init__(
        self,
        service: LlmService,
        config: LlmCallConfig,
        credentials: LlmCredentials,
        *,
        agent_id: str = "",
        session_id: str = "",
        turn_id: str = "",
        cancellation: asyncio.Event | None = None,
    ) -> None:
        self._service = service
        self._config = config
        self._credentials = credentials
        self._agent_id = agent_id
        self._session_id = session_id
        self._turn_id = turn_id
        self._cancellation = cancellation or asyncio.Event()
        self.provider = config.provider
        self.model = config.model
        self.api_type = config.api_type

    async def stream(self, messages, tools=None):
        request = LlmRequest.from_parts(
            self._config,
            messages,
            tools,
            purpose="conversation",
            agent_id=self._agent_id,
            session_id=self._session_id,
            turn_id=self._turn_id,
            cancellation=self._cancellation,
        )
        async for chunk in self._service.stream(
            request,
            credentials=self._credentials,
            dispatch_stream_hooks=False,
        ):
            # 这里不做 isinstance、复制或字段裁剪；协议对象就是 Package 的产物。
            yield chunk

    def cancel(self) -> None:
        self._cancellation.set()


__all__ = ["LlmServiceAdapter"]
