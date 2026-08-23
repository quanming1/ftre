"""In-memory Team Feature state; member execution stays in Agent services."""
# TeamService：保存 leader、成员和消息路由等团队元数据；
# 它不创建 AgentLoop、不持久化聊天历史——执行与存储都在 Agent 服务层。

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Team:
    """团队元数据：leader Session 与成员 Agent 的可见映射。"""
    team_id: str
    leader_session_id: str
    members: dict[str, str] = field(default_factory=dict)


class TeamService:
    """Manage Team metadata without owning Inbox or AgentLoop internals."""
    key = "teams"

    def __init__(self) -> None:
        self._teams: dict[str, Team] = {}

    def create(self, team_id: str, leader_session_id: str) -> Team:
        """创建团队；同一 team_id 不能覆盖已有团队。"""
        if team_id in self._teams:
            raise ValueError(f"team {team_id!r} already exists")
        team = Team(team_id, leader_session_id)
        self._teams[team_id] = team
        return team

    def get(self, team_id: str) -> Team | None:
        """读取团队元数据，不返回 Agent 执行对象。"""
        return self._teams.get(team_id)

    def delete(self, team_id: str) -> bool:
        """删除团队状态并报告是否确实存在。"""
        return self._teams.pop(team_id, None) is not None

    def snapshot(self) -> tuple[Team, ...]:
        """返回当前团队状态的不可变容器快照。"""
        return tuple(self._teams.values())
