"""工具权限值模型；决策引擎属于 Host ToolService。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PermissionBehavior(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class PermissionRule(BaseModel):
    id: str
    tool_name: str
    argument_regex: dict[str, str] = Field(default_factory=dict)
    behavior: PermissionBehavior
    priority: int = 0
    enabled: bool = True


class PermissionDecision(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    behavior: PermissionBehavior
    reason: str
    rule_id: str | None = None


class PermissionContext(BaseModel):
    permission_rules: list[PermissionRule] = Field(default_factory=list)
    default_behavior: PermissionBehavior = PermissionBehavior.ALLOW


__all__ = [
    "PermissionBehavior",
    "PermissionContext",
    "PermissionDecision",
    "PermissionRequest",
    "PermissionRule",
]
