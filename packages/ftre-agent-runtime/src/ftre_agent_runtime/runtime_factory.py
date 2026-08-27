"""把私有 AgentLoop 包装成 AgentService 可消费的 RuntimeFactory。"""

from __future__ import annotations

from typing import Any

from ftre_agent import AgentRuntimeFactory, InboundMessage


class AgentLoopFactory(AgentRuntimeFactory):
    """Runtime Package 的唯一公开注册对象，不把 AgentLoop 直接暴露给 Host。"""

    name = "ftre-agent-runtime"
    version = "0.1.0"

    def __init__(self, loop: Any) -> None:
        self._loop = loop

    async def run_inbound(self, message: InboundMessage):
        return await self._loop.run_inbound(message)

    async def cancel_session(self, *args: Any, **kwargs: Any):
        return await self._loop.cancel_session(*args, **kwargs)

    def get_session_status(self, session_id: str) -> str:
        return self._loop.get_session_status(session_id)

    def is_active_session(self, session_id: str) -> bool:
        return self._loop.is_active_session(session_id)

    async def delete_session(self, session_id: str):
        return await self._loop.delete_session(session_id)

    async def resume_confirmation(self, *args: Any, **kwargs: Any):
        return await self._loop.resume_confirmation(*args, **kwargs)

    async def stop(self) -> None:
        await self._loop.stop()

    def start(self) -> None:
        self._loop.start()


__all__ = ["AgentLoopFactory"]
