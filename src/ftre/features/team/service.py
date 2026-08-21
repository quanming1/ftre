"""In-memory Team Feature state; member execution stays in Agent services."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Team:
    team_id: str
    leader_session_id: str
    members: dict[str, str] = field(default_factory=dict)


class TeamService:
    """Manage Team metadata without owning SessionLane or AgentLoop internals."""
    key = "teams"

    def __init__(self) -> None:
        self._teams: dict[str, Team] = {}

    def create(self, team_id: str, leader_session_id: str) -> Team:
        if team_id in self._teams:
            raise ValueError(f"team {team_id!r} already exists")
        team = Team(team_id, leader_session_id)
        self._teams[team_id] = team
        return team

    def get(self, team_id: str) -> Team | None:
        return self._teams.get(team_id)

    def delete(self, team_id: str) -> bool:
        return self._teams.pop(team_id, None) is not None

    def snapshot(self) -> tuple[Team, ...]:
        return tuple(self._teams.values())
