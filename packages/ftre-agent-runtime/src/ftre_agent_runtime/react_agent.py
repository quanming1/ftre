"""Runtime Agent：持有 ReAct 状态并委托唯一 Runtime Runner。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from ftre_agent.hooks import HookDispatcher
from ftre_agent.message import Msg
from ftre_agent.tracing import Tracer
from ftre_llm import LlmAdapter

from .agent_state import AgentState
from .message_context import MessageContext
from .react_runner import ReActRunner


class ReActAgent:
    """无 Host 依赖的 ReAct Agent 实现。

    Host 通过构造参数注入 LlmAdapter、ToolView 和 HookDispatcher；Agent 不知道
    Session、Inbox、Channel 或 ToolService 的具体实现。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        api_base: str | None = None,
        api_type: str = "completions",
        provider: str = "",
        system_prompt: str = "",
        tool_view=None,
        max_iterations: int | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str = "",
        state: AgentState | None = None,
        max_retries: int = 5,
        retry_delay: float = 3.0,
        tracer: Tracer | None = None,
        hooks: HookDispatcher | None = None,
        hook_context: object | None = None,
        llm: LlmAdapter | None = None,
    ) -> None:
        if tool_view is None:
            raise TypeError("Runtime Agent requires an injected ToolView")
        if llm is None:
            raise TypeError("Runtime Agent requires an injected LLM adapter")
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.api_type = api_type
        self.provider = provider
        self._system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.tracer = tracer or Tracer()
        self.hooks = hooks
        self.hook_context = hook_context
        self._state = state or AgentState()
        self._tool_view = tool_view
        self._runner = ReActRunner(self, llm=llm)

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        self._system_prompt = value

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def run_state(self):
        return self._runner.state

    @property
    def messages(self) -> list[dict]:
        return MessageContext.messages(self._state.context)

    @property
    def tool_view(self):
        return self._tool_view

    @property
    def runner(self) -> ReActRunner:
        return self._runner

    async def run(
        self, message: str | Msg | list[Msg], runtime_context: dict | None = None
    ) -> AsyncGenerator[Any, None]:
        async for event in self._runner.run(message, runtime_context=runtime_context):
            yield event

    def cancel_nowait(self) -> None:
        self._runner.cancel_nowait()

    def build_final_assistant_events(self):
        """在 Turn 语义边界生成本次新增/变更的 whole-value 快照。"""
        return self._runner.build_final_assistant_events()

    def build_final_assistant_event(self):
        """兼容只消费一条快照的宿主，返回最后一条。"""
        return self._runner.build_final_assistant_event()


__all__ = ["ReActAgent"]
