from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"
PACKAGE = ROOT / "packages" / "ftre-compaction"


def test_core_has_no_compaction_owner_or_context_gate():
    assert not (SRC / "services" / "compaction").exists()
    assert not (SRC / "features" / "compaction").exists()
    assert not (SRC / "services" / "agent_loop" / "runtime" / "loop" / "context_gate.py").exists()

    for path in (
        SRC / "services" / "agent_loop" / "runtime" / "mailbox" / "lane.py",
        SRC / "services" / "agent_loop" / "runtime" / "loop" / "engine.py",
        SRC / "services" / "agent_loop" / "provider.py",
        SRC / "services" / "command" / "builtin.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "CompactionService" not in source
        assert "ContextGate" not in source
        assert "ftre.services.compaction" not in source


def test_optional_package_is_single_compaction_owner():
    assert (PACKAGE / "pyproject.toml").exists()
    assert (PACKAGE / "src" / "ftre_compaction" / "service.py").exists()
    assert (PACKAGE / "src" / "ftre_compaction" / "hooks.py").exists()
    assert (PACKAGE / "src" / "ftre_compaction" / "commands.py").exists()
    source = (PACKAGE / "src" / "ftre_compaction" / "hooks.py").read_text(encoding="utf-8")
    assert "AGENT_PRE_STEP_SPEC" in source
    assert "AGENT_AFTER_TURN_SPEC" in source
    assert "AGENT_REQUEST_ERROR_SPEC" in source


def test_core_agent_config_does_not_own_compaction_settings():
    source = (SRC / "services" / "agent" / "config.py").read_text(encoding="utf-8")
    context_block = source.split("class ContextConfig:", 1)[1].split(
        "class AgentConfig:", 1
    )[0]
    for field in (
        "precompact_threshold",
        "compact_threshold",
        "consolidation_ratio",
        "safety_buffer",
        "compact_llm",
    ):
        assert field not in context_block
    assert "compact_generation" not in source
    assert (PACKAGE / "src" / "ftre_compaction" / "config.py").exists()


def test_composition_does_not_enable_compaction_by_default():
    source = (SRC / "app" / "gateway" / "composition.py").read_text(encoding="utf-8")
    assert "ftre.services.compaction" not in source
    assert "ftre.features.compaction" not in source
