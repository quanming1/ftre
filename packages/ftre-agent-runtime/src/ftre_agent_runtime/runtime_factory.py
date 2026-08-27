"""把私有 AgentLoop 包装成 AgentService 可消费的 RuntimeFactory。"""

from __future__ import annotations

from typing import Any

from ftre_agent import (
    AgentCreateSpec,
    AgentEvent,
    AgentResumeSpec,
    AgentRunRequest,
    AgentRuntimeFactory,
    InboundMessage,
)


class AgentLoopHandle:
    """绑定 Agent 配置的 Runtime Handle；实际执行仍由共享 AgentLoop 完成。"""

    def __init__(self, factory: AgentLoopFactory, spec: AgentCreateSpec | AgentResumeSpec):
        self._factory = factory
        self.spec = spec

    async def run(self, request: AgentRunRequest):
        return await self._factory.run_request(request)

    async def stream(self, request: AgentRunRequest):
        result = await self.run(request)
        yield AgentEvent(
            event_type="run.completed",
            agent_id=self.spec.agent_id,
            run_id=result.run_id or result.turn_id,
            sequence=0,
            data={"status": result.status},
        )

    async def cancel(self, reason: str = ""):
        del reason
        return await self._factory.cancel_session(self.spec.session_id or self.spec.agent_id)

    async def dispose(self) -> None:
        return None


class AgentLoopFactory(AgentRuntimeFactory):
    """Runtime Package 的唯一公开注册对象，不把 AgentLoop 直接暴露给 Host。"""

    name = "ftre-agent-runtime"
    version = "0.1.0"

    def __init__(self, loop: Any) -> None:
        self._loop = loop

    async def create(self, spec: AgentCreateSpec) -> AgentLoopHandle:
        return AgentLoopHandle(self, spec)

    async def resume(self, spec: AgentResumeSpec) -> AgentLoopHandle:
        return AgentLoopHandle(self, spec)

    async def run_request(self, request: AgentRunRequest):
        return await self._loop.run_inbound(self._to_inbound(request))

    @staticmethod
    def _to_inbound(request: AgentRunRequest) -> InboundMessage:
        texts = [message.get_text_content() or "" for message in request.messages]
        metadata = dict(request.metadata)
        attachments = metadata.pop("attachments", ())
        metadata["agent_id"] = str(
            metadata.get("profile_agent_id")
            or metadata.get("runtime_agent_id")
            or request.agent_id
            or "default"
        )
        return InboundMessage(
            session_id=request.session_id,
            request_id=request.request_id,
            channel_id=request.channel_id,
            content="\n".join(text for text in texts if text),
            attachments=tuple(dict(item) for item in attachments if isinstance(item, dict)),
            source=request.source,
            metadata=metadata,
        )

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


__all__ = ["AgentLoopFactory", "AgentLoopHandle"]
