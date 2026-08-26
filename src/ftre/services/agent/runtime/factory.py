"""Agent Runtime 私有的 Core Agent 构造。

Profile Service 只解析配置，Tool Service 只准备工具视图；真正实例化 Core
ReActAgent 由这一处完成，避免 AgentManager 和 Runtime 各自拥有一套工厂。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from ftre_agent_core.agent import AgentState, ReActAgent
from ftre_agent_core.permission import PermissionBehavior, PermissionContext


def default_agent_state() -> AgentState:
    """创建默认权限状态；规则仍由 Core 的 AgentState 消费。"""
    return AgentState(
        permission_context=PermissionContext(
            permission_rules=[],
            default_behavior=PermissionBehavior.ALLOW,
        )
    )


def compose_system_prompt(config, profile, *, channel_id: str, session_id: str) -> str:
    """合并已有 PromptAssembly 文本、Profile 提示词和环境事实。"""
    system_prompt = config.system_prompt
    if profile is not None:
        if profile.soul_prompt:
            soul_path = f"{profile.agent_dir}/SOUL.md" if profile.agent_dir else ""
            system_prompt += (
                "\n\n"
                f'<SOUL desc="智能体人设：角色定义、语气、行为边界" path="{soul_path}">\n'
                f"{profile.soul_prompt}\n</SOUL>"
            )
        if profile.user_prompt_md:
            user_path = f"{profile.agent_dir}/USER.md" if profile.agent_dir else ""
            system_prompt += (
                "\n\n"
                f'<USER_PROFILE desc="用户偏好与个人要求" path="{user_path}">\n'
                f"{profile.user_prompt_md}\n</USER_PROFILE>"
            )

    env_lines = [
        "<FTRE_SYSTEM_FACT>",
        "<env>",
        f"channel_id={channel_id}",
        f"session_id={session_id}",
        f"os={os.name}",
        f"date={datetime.now(UTC).date().isoformat()}",
    ]
    if os.name == "nt":
        env_lines.append(
            "当前是 Windows 系统。书写路径时优先使用正斜杠 /；如果必须用反斜杠，"
            "在 JSON/字符串里务必写成双反斜杠 \\\\，避免路径被转义。"
        )
    else:
        env_lines.append(
            "当前是类 Unix 系统（Linux/macOS）。路径使用正斜杠 /，优先使用绝对路径。"
        )
    if getattr(config.llm, "vision", False):
        env_lines.append(
            "vision=true：当前模型具备识图能力，可使用 read 工具读取图片和截图。"
        )
    env_lines.extend(("</env>", "</FTRE_SYSTEM_FACT>"))
    return system_prompt + "\n\n" + "\n".join(env_lines)


def create_core_agent(
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
    """使用已解析快照构造唯一的 Core ReActAgent。"""
    profile = profile_snapshot
    llm_config = profile.llm if profile is not None else config.llm
    return ReActAgent(
        model=llm_config.model,
        api_key=llm_config.api_key,
        api_base=llm_config.api_base,
        api_type=llm_config.api_type,
        system_prompt=system_prompt,
        tool_registry=tool_view,
        max_iterations=config.max_iterations,
        max_tokens=llm_config.max_output,
        reasoning_effort=llm_config.reasoning_effort,
        tracer=tracer,
        hooks=hooks,
        hook_context=hook_context,
        state=state,
        llm=llm,
    )


__all__ = ["compose_system_prompt", "create_core_agent", "default_agent_state"]
