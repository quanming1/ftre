"""
ContextConfig 加载逻辑测试。

不依赖真实 ~/.ftre/config.json；通过 monkeypatch 注入 fake config dict。
"""
from __future__ import annotations

import pytest

from ftre import config as ftre_config
from ftre.config import AgentConfig, ContextConfig


@pytest.fixture
def fake_config(monkeypatch):
    """让 load_config_file 返回我们指定的 dict。"""
    holder: dict = {}

    def _make(data: dict) -> AgentConfig:
        holder["data"] = data
        monkeypatch.setattr(ftre_config, "load_config_file", lambda: data)
        return ftre_config.load_config()

    return _make


def test_context_defaults_when_missing(fake_config):
    cfg = fake_config({"agents": {"defaults": {"model": "x", "provider": "y"}}})
    assert isinstance(cfg.context, ContextConfig)
    assert cfg.context.precompact_threshold == 0.5
    assert cfg.context.compact_threshold == 0.6
    assert cfg.context.consolidation_ratio == 0.5
    assert cfg.context.safety_buffer == 1024
    assert cfg.context.idle_compaction is True
    assert cfg.context.silent is True


def test_context_camel_case(fake_config):
    cfg = fake_config({
        "agents": {
            "defaults": {
                "context": {
                    "precompactThreshold": 0.45,
                    "compactThreshold": 0.7,
                    "consolidationRatio": 0.4,
                    "safetyBuffer": 2048,
                    "idleCompaction": False,
                    "silent": False,
                }
            }
        }
    })
    assert cfg.context.precompact_threshold == 0.45
    assert cfg.context.compact_threshold == 0.7
    assert cfg.context.consolidation_ratio == 0.4
    assert cfg.context.safety_buffer == 2048
    assert cfg.context.idle_compaction is False
    assert cfg.context.silent is False


def test_context_snake_case_also_works(fake_config):
    cfg = fake_config({
        "agents": {
            "defaults": {
                "context": {
                    "precompact_threshold": 0.4,
                    "compact_threshold": 0.8,
                    "consolidation_ratio": 0.6,
                    "safety_buffer": 512,
                }
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
            "defaults": {
                "context": {
                    "threshold": 0.75,
                }
            }
        }
    })
    assert cfg.context.precompact_threshold == 0.5
    assert cfg.context.compact_threshold == 0.75


def test_context_camel_takes_precedence_over_snake(fake_config):
    cfg = fake_config({
        "agents": {
            "defaults": {
                "context": {
                    "consolidationRatio": 0.5,
                    "consolidation_ratio": 0.9,  # 应被 camelCase 覆盖
                }
            }
        }
    })
    assert cfg.context.consolidation_ratio == 0.5


def test_context_invalid_payload_falls_back_to_defaults(fake_config):
    cfg = fake_config({"agents": {"defaults": {"context": "not-a-dict"}}})
    assert cfg.context.consolidation_ratio == 0.5  # 默认


def test_load_config_with_no_data_returns_default_agent_config(monkeypatch):
    monkeypatch.setattr(ftre_config, "load_config_file", lambda: {})
    cfg = ftre_config.load_config()
    assert isinstance(cfg, AgentConfig)
    assert isinstance(cfg.context, ContextConfig)
    # 默认值
    assert cfg.context.idle_compaction is True
