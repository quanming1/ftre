"""Agent profile 合并后的最小公开投影模型。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ftre_agent import AgentConfig, LLMConfig


@dataclass(frozen=True)
class EffectiveProfile:
    """一个 Agent 在当前请求中解析出的最终配置视图。"""
    agent_id: str
    value: Any


@dataclass(frozen=True, slots=True)
class ProfileQuery:
    """Profile 解析请求；路径由调用者提供，Service 不读取外部全局状态。"""

    name: str = "default"
    project_root: str | None = None
    user_root: str | None = None
    global_config_path: str | None = None
    workspace: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class FrozenLLMConfig:
    """LLMConfig 的不可变 Profile 快照值。"""

    provider: str = ""
    api_key: str = ""
    api_base: str = ""
    api_type: str = "completions"
    name: str = ""
    id: str = ""
    context_window: int | None = None
    max_output: int | None = None
    vision: bool = False
    reasoning_effort: str = ""
    reasoning_effort_values: tuple[str, ...] = ()
    model: str = ""

    @classmethod
    def from_config(cls, value: LLMConfig | None) -> FrozenLLMConfig:
        value = value or LLMConfig()
        return cls(
            provider=value.provider,
            api_key=value.api_key,
            api_base=value.api_base,
            api_type=value.api_type,
            name=value.name,
            id=value.id,
            context_window=value.context_window,
            max_output=value.max_output,
            vision=value.vision,
            reasoning_effort=value.reasoning_effort,
            reasoning_effort_values=tuple(value.reasoning_effort_values),
            model=value.model,
        )

    def to_config(self) -> LLMConfig:
        return LLMConfig(
            provider=self.provider,
            api_key=self.api_key,
            api_base=self.api_base,
            api_type=self.api_type,
            name=self.name,
            id=self.id,
            context_window=self.context_window,
            max_output=self.max_output,
            vision=self.vision,
            reasoning_effort=self.reasoning_effort,
            reasoning_effort_values=self.reasoning_effort_values,
            model=self.model,
        )


@dataclass(frozen=True, slots=True)
class AgentProfileSnapshot:
    """创建 Agent 时冻结的 Profile 快照。"""

    name: str
    snapshot_hash: str
    llm: FrozenLLMConfig
    prompt_sources: Any
    tool_policy: Any
    workspace: str
    source_trace: tuple[str, ...]
    metadata: Any

    def to_agent_config(self) -> AgentConfig:
        """把快照转换为 AgentService 可消费的配置值。"""
        return AgentConfig(llm=self.llm.to_config(), workspace=self.workspace)


def freeze_profile(profile: Any, *, query: ProfileQuery) -> AgentProfileSnapshot:
    """将旧 Manager 结果转换为深冻结、可哈希的 Profile 快照。"""
    prompt_sources = MappingProxyType(
        {
            "SOUL.md": str(getattr(profile, "soul_prompt", "") or ""),
            "AGENTS.md": str(getattr(profile, "agents_md", "") or ""),
            "USER.md": str(getattr(profile, "user_prompt_md", "") or ""),
        }
    )
    raw_policy = getattr(profile, "tools_config", None) or {}
    policy = MappingProxyType(
        {
            "allow": tuple(raw_policy.get("allow", ())) if isinstance(raw_policy, dict) else (),
            "deny": tuple(raw_policy.get("deny", ())) if isinstance(raw_policy, dict) else (),
        }
    )
    workspace = query.workspace or str(getattr(profile, "workspace", "") or "")
    trace = tuple(
        value
        for value in (
            str(getattr(profile, "agent_dir", "") or ""),
            query.global_config_path or "",
            query.project_root or "",
        )
        if value
    )
    metadata = MappingProxyType(dict(query.metadata))
    digest = hashlib.sha256(
        json.dumps(
            {
                "name": str(getattr(profile, "agent_id", query.name)),
                "llm": getattr(getattr(profile, "llm", None), "__dict__", {}),
                "prompt_sources": dict(prompt_sources),
                "tool_policy": {key: list(value) for key, value in policy.items()},
                "workspace": workspace,
                "trace": trace,
                "metadata": dict(metadata),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return AgentProfileSnapshot(
        name=str(getattr(profile, "agent_id", query.name)),
        snapshot_hash=digest,
        llm=FrozenLLMConfig.from_config(getattr(profile, "llm", None)),
        prompt_sources=prompt_sources,
        tool_policy=policy,
        workspace=workspace,
        source_trace=trace,
        metadata=metadata,
    )


__all__ = [
    "AgentProfileSnapshot",
    "EffectiveProfile",
    "FrozenLLMConfig",
    "ProfileQuery",
    "freeze_profile",
]
