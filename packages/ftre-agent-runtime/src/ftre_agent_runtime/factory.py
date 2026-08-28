"""Agent Runtime 私有的 ReAct Agent 构造。

Profile Service 只解析配置，System Prompt Service 负责完整 Prompt 组装，
Tool Service 只准备工具视图；真正实例化 ReAct Agent 由这一处完成。
"""

from __future__ import annotations

from typing import Any

from ftre_agent.tool.permission import PermissionBehavior, PermissionContext

from .agent_state import AgentState
from .react_agent import ReActAgent


def default_agent_state() -> AgentState:
    """创建 Runtime Agent 的默认状态快照。"""
    return AgentState(
        permission_context=PermissionContext(
            permission_rules=[],
            default_behavior=PermissionBehavior.ALLOW,
        )
    )


def create_runtime_agent(
    config,
    profile_snapshot: Any,
    tool_view,
    system_prompt: str,
    tracer,
    hooks,
    hook_context,
    state: AgentState,
    llm=None,
):
    """使用已解析快照构造唯一的 Runtime ReActAgent。"""
    profile = profile_snapshot
    llm_config = profile.llm if profile is not None else config.llm
    return ReActAgent(
        model=llm_config.model,
        api_key=llm_config.api_key,
        api_base=llm_config.api_base,
        api_type=llm_config.api_type,
        system_prompt=system_prompt,
        provider=llm_config.provider,
        tool_view=tool_view,
        max_iterations=config.max_iterations,
        max_tokens=llm_config.max_output,
        reasoning_effort=llm_config.reasoning_effort,
        tracer=tracer,
        hooks=hooks,
        hook_context=hook_context,
        state=state,
        llm=llm,
    )


__all__ = ["create_runtime_agent", "default_agent_state"]
