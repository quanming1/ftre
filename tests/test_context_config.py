"""
ContextConfig 加载逻辑测试。

不依赖真实 ~/.ftre/config.json；通过 monkeypatch 注入 fake config dict。
"""
from __future__ import annotations

import pytest

from ftre import config as ftre_config
from ftre.config import AgentConfig, ContextConfig, LLMConfig, sanitize_agent_effort


@pytest.fixture
def fake_config(monkeypatch):
    """让 load_config_file 返回我们指定的 dict。"""
    holder: dict = {}

    def _make(data: dict) -> AgentConfig:
        holder["data"] = data
        monkeypatch.setattr(ftre_config, "load_config_file", lambda: data)
        # 清除缓存，确保 load_config() 重新解析
        monkeypatch.setattr(ftre_config, "_last_config", None)
        monkeypatch.setattr(ftre_config, "_last_sig", "")
        # 避免读取真实 ~/.ftre/agents/default/agent.config.json
        monkeypatch.setattr(ftre_config, "_read_default_agent_llm", lambda: ("", "", ""))
        monkeypatch.setattr(ftre_config, "_read_default_agent_reasoning_effort", lambda: None)
        return ftre_config.load_config()

    return _make


def test_default_agent_reasoning_effort_overrides_model_default(monkeypatch):
    data = {
        "providers": {
            "openai": {
                "models": [{"id": "gpt-test", "reasoning_effort": "high"}],
            },
        },
    }
    monkeypatch.setattr(ftre_config, "load_config_file", lambda: data)
    monkeypatch.setattr(ftre_config, "_last_config", None)
    monkeypatch.setattr(ftre_config, "_last_sig", "")
    monkeypatch.setattr(ftre_config, "_read_default_agent_llm", lambda: ("openai", "gpt-test", ""))
    monkeypatch.setattr(ftre_config, "_read_default_agent_reasoning_effort", lambda: "")

    assert ftre_config.load_config().llm.reasoning_effort == ""


def test_sanitize_agent_effort_dropped_when_model_unsupported():
    """模型完全没声明推理强度（无默认值也无可选值）时丢弃非空 effort。"""
    llm = LLMConfig(reasoning_effort="", reasoning_effort_values=())
    assert sanitize_agent_effort("max", llm) == ""


def test_sanitize_agent_effort_kept_when_model_has_default():
    """模型声明了默认 effort（如 high）→ 保留 agent 配置。"""
    llm = LLMConfig(reasoning_effort="high", reasoning_effort_values=())
    assert sanitize_agent_effort("max", llm) == "max"


def test_sanitize_agent_effort_kept_when_model_has_values():
    """模型声明了可选值列表 → 保留 agent 配置。"""
    llm = LLMConfig(
        reasoning_effort="none",
        reasoning_effort_values=("none", "low", "medium", "high", "max"),
    )
    assert sanitize_agent_effort("high", llm) == "high"


def test_sanitize_agent_effort_empty_effort_stays_empty():
    """空 effort（未设置/已清空）保持为空。"""
    llm = LLMConfig(reasoning_effort="", reasoning_effort_values=())
    assert sanitize_agent_effort("", llm) == ""


def test_default_agent_effort_dropped_for_model_without_reasoning_config(monkeypatch):
    """default agent 显式配了 effort，但目标模型未声明推理强度 → 清空，避免请求 400。"""
    data = {
        "providers": {
            "openai": {
                "models": [{"id": "no-think", "name": "No Think"}],
            },
        },
    }
    monkeypatch.setattr(ftre_config, "load_config_file", lambda: data)
    monkeypatch.setattr(ftre_config, "_last_config", None)
    monkeypatch.setattr(ftre_config, "_last_sig", "")
    monkeypatch.setattr(ftre_config, "_read_default_agent_llm", lambda: ("openai", "no-think", ""))
    monkeypatch.setattr(ftre_config, "_read_default_agent_reasoning_effort", lambda: "max")

    assert ftre_config.load_config().llm.reasoning_effort == ""


def test_default_agent_effort_kept_for_model_with_reasoning_config(monkeypatch):
    """目标模型声明了推理强度（有 values）→ default agent 的 effort 保留。"""
    data = {
        "providers": {
            "openai": {
                "models": [
                    {
                        "id": "thinker",
                        "name": "Thinker",
                        "reasoning_effort": "none",
                        "reasoning_effort_values": ["none", "low", "high", "max"],
                    },
                ],
            },
        },
    }
    monkeypatch.setattr(ftre_config, "load_config_file", lambda: data)
    monkeypatch.setattr(ftre_config, "_last_config", None)
    monkeypatch.setattr(ftre_config, "_last_sig", "")
    monkeypatch.setattr(ftre_config, "_read_default_agent_llm", lambda: ("openai", "thinker", ""))
    monkeypatch.setattr(ftre_config, "_read_default_agent_reasoning_effort", lambda: "max")

    assert ftre_config.load_config().llm.reasoning_effort == "max"


def test_build_llm_config_reads_reasoning_effort_values():
    """_build_llm_config 从模型条目读取 reasoning_effort_values。"""
    data = {
        "providers": {
            "openai": {
                "api_key": "sk",
                "api_base": "https://x",
                "models": [
                    {"id": "m1", "reasoning_effort_values": ["low", "high"]},
                    {"id": "m2"},
                ],
            },
        },
    }
    assert ftre_config._build_llm_config(data, "openai", "m1").reasoning_effort_values == (
        "low",
        "high",
    )
    assert ftre_config._build_llm_config(data, "openai", "m2").reasoning_effort_values == ()


def test_context_defaults_when_missing(fake_config):
    cfg = fake_config({"agents": {"title_generation": {"provider": "x", "model": "y"}}})
    assert isinstance(cfg.context, ContextConfig)
    assert cfg.context.precompact_threshold == 0.7
    assert cfg.context.compact_threshold == 0.8
    assert cfg.context.consolidation_ratio == 0.5
    assert cfg.context.safety_buffer == 1024


def test_context_camel_case(fake_config):
    cfg = fake_config({
        "agents": {
            "context": {
                "precompactThreshold": 0.45,
                "compactThreshold": 0.7,
                "consolidationRatio": 0.4,
                "safetyBuffer": 2048,
            }
        }
    })
    assert cfg.context.precompact_threshold == 0.45
    assert cfg.context.compact_threshold == 0.7
    assert cfg.context.consolidation_ratio == 0.4
    assert cfg.context.safety_buffer == 2048


def test_context_snake_case_also_works(fake_config):
    cfg = fake_config({
        "agents": {
            "context": {
                "precompact_threshold": 0.4,
                "compact_threshold": 0.8,
                "consolidation_ratio": 0.6,
                "safety_buffer": 512,
            }
        }
    })
    assert cfg.context.precompact_threshold == 0.4
    assert cfg.context.compact_threshold == 0.8
    assert cfg.context.consolidation_ratio == 0.6
    assert cfg.context.safety_buffer == 512


def test_context_legacy_threshold_maps_to_compact_threshold(fake_config):
    cfg = fake_config({
        "agents": {
            "context": {
                "threshold": 0.75,
            }
        }
    })
    assert cfg.context.precompact_threshold == 0.7
    assert cfg.context.compact_threshold == 0.75


def test_context_camel_takes_precedence_over_snake(fake_config):
    cfg = fake_config({
        "agents": {
            "context": {
                "consolidationRatio": 0.5,
                "consolidation_ratio": 0.9,  # 应被 camelCase 覆盖
            }
        }
    })
    assert cfg.context.consolidation_ratio == 0.5


def test_context_invalid_payload_falls_back_to_defaults(fake_config):
    cfg = fake_config({"agents": {"context": "not-a-dict"}})
    assert cfg.context.consolidation_ratio == 0.5  # 默认


def test_load_config_with_no_data_returns_default_agent_config(monkeypatch):
    monkeypatch.setattr(ftre_config, "load_config_file", lambda: {})
    cfg = ftre_config.load_config()
    assert isinstance(cfg, AgentConfig)
    assert isinstance(cfg.context, ContextConfig)
