"""Skill Feature 的不可变目录记录模型。

类型只描述技能名称、正文、来源、优先级和 scope，不负责扫描文件或决定 winner；
这些规则由 SkillService 统一执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SkillRecord:
    """技能目录中一次已解析的最终候选记录。

    ``owner/source/priority/scope`` 用于解释 winner 来源；记录本身不负责读取
    文件，也不允许在组装后被 Feature 修改。
    """
    name: str
    content: str
    owner: str
    source: str
    priority: int
    scope: str
    description: str = ""
    kind: str = "file"
    updated_at: float = 0.0
    disabled: bool = False
    user_invocable: bool = True
    model_invocable: bool = True
    path: str = ""
    metadata: dict[str, Any] | None = None
    when_to_use: str | None = None
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: str | None = None
