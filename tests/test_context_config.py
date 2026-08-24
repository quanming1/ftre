"""核心 Agent 配置不再拥有 Inbox/Compaction 配置。"""

from __future__ import annotations

from ftre.services.agent.config import (
    AgentConfig,
    LLMConfig,
    load_config,
    sanitize_agent_effort,
)


def test_agent_config_has_no_queue_owner() -> None:
    config = AgentConfig()
    assert not hasattr(config, "context")
    assert not hasattr(config, "mailbox_capacity")


def test_load_config_ignores_package_owned_context_fields(monkeypatch) -> None:
    from ftre.services.agent import config as module

    monkeypatch.setattr(module, "load_config_file", lambda: {"agents": {"context": {"mailboxCapacity": 42}}})
    monkeypatch.setattr(module, "_last_config", None)
    monkeypatch.setattr(module, "_last_sig", "")
    monkeypatch.setattr(module, "_read_default_agent_llm", lambda: ("", "", ""))
    monkeypatch.setattr(module, "_read_default_agent_reasoning_effort", lambda: None)
    assert isinstance(load_config(), AgentConfig)


def test_sanitize_agent_effort_keeps_model_contract() -> None:
    assert sanitize_agent_effort("max", LLMConfig(reasoning_effort="high")) == "max"
    assert sanitize_agent_effort("max", LLMConfig()) == ""
