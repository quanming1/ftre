"""Agent Profile 公共 Service。

配置文件、默认 profile 和 prompt 文件由 ``AgentManager`` 持有；这里把它收敛成
Feature 可消费的窄接口，调用方不需要知道 profile 在磁盘上的布局和校验规则。
"""

from __future__ import annotations

from typing import Any

from .manager import AgentManager
from .models import EffectiveProfile


class AgentProfileService:
    """提供 profile CRUD/解析，但不向 Feature 暴露 Manager 的存储细节。"""
    key = "agent_profiles"

    def __init__(self, manager: AgentManager) -> None:
        self.manager = manager

    def list(self) -> list[dict[str, Any]]:
        """列出可用 Agent profile 的摘要。"""
        return self.manager.list_agents()

    def get(self, agent_id: str):
        """读取一个 profile；不存在时由 Manager 返回空值/抛出领域错误。"""
        return self.manager.load(agent_id)

    def create(self, **kwargs: Any):
        """创建 profile，并由 Manager 负责默认值和磁盘校验。"""
        return self.manager.create_agent_profile(**kwargs)

    def update(self, agent_id: str, payload: dict[str, Any]):
        """更新指定 profile 的可编辑配置。"""
        return self.manager.update_agent(agent_id, payload)

    def delete(self, agent_id: str) -> None:
        """删除 profile；不会由 Service 直接操作 profile 目录。"""
        self.manager.delete_agent(agent_id)

    def resolve(self, agent_id: str, session_id: str | None = None) -> EffectiveProfile:
        """解析当前 Agent 的有效配置投影，供一次请求使用。"""
        return EffectiveProfile(agent_id, self.manager.load(agent_id))

    def list_prompts(self, agent_id: str) -> dict[str, str]:
        """Read prompt files through the profile owner."""
        return self.manager.read_prompts(agent_id)

    def update_prompt(self, agent_id: str, filename: str, content: str) -> None:
        """Write one allow-listed prompt file through the profile owner."""
        self.manager.write_prompt(agent_id, filename, content)

    # 团队 Package 只能注入这个公开 Service，不能 import profile 目录下的私有存储
    # helper。成员 profile 仍由 Agent Profile Owner 负责路径、校验、落盘和清理。
    def write_team_member_profile(
        self,
        session_manager,
        leader_session_id: str,
        member_session_id: str,
        role: str,
        overrides: dict,
    ):
        """为 Team 成员写入与全局 Agent 同构的 profile。"""
        from . import sub_agent

        return sub_agent.write_member_profile(
            session_manager,
            leader_session_id,
            member_session_id,
            role=role,
            overrides=overrides,
        )

    def delete_team_member_profile(
        self, session_manager, leader_session_id: str, member_session_id: str
    ) -> bool:
        """删除一个 Team 成员 profile，保持成员目录清理的唯一 Owner。"""
        from . import sub_agent

        return sub_agent.delete_member_profile(
            session_manager, leader_session_id, member_session_id
        )

    @staticmethod
    def build_team_member_binding(
        leader_session_id: str, team_id: str, name: str
    ) -> dict:
        """构造 Team 成员 metadata 绑定，保持绑定形状稳定。"""
        from . import sub_agent

        return sub_agent.build_team_member_binding(leader_session_id, team_id, name)
