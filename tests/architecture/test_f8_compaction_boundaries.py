from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"
PACKAGE = ROOT / "packages" / "ftre-compaction"


def test_core_has_no_compaction_owner_or_context_gate():
    assert not (SRC / "services" / "compaction").exists()
    assert not (SRC / "plugins" / "builtin" / "compaction").exists()
    assert not (SRC / "services" / "agent_loop" / "runtime" / "loop" / "context_gate.py").exists()

    runtime_root = ROOT / "packages" / "ftre-agent-runtime" / "src" / "ftre_agent_runtime"
    for path in (
        runtime_root / "engine.py",
        runtime_root / "plugin.py",
        SRC / "plugins" / "builtin" / "command" / "builtin.py",
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
    assert "INBOX_BEFORE_CLAIM_SPEC" in source
    assert "AGENT_AFTER_RUN_SPEC" in source
    assert "AGENT_RUN_ERROR_SPEC" in source


def test_core_agent_config_does_not_own_compaction_settings():
    source = (SRC / "services" / "agent_profile" / "config.py").read_text(encoding="utf-8")
    for field in (
        "precompact_threshold",
        "compact_threshold",
        "consolidation_ratio",
        "safety_buffer",
        "compact_llm",
    ):
        assert field not in source
    assert "mailbox_capacity" not in source
    assert "compact_generation" not in source
    assert (PACKAGE / "src" / "ftre_compaction" / "config.py").exists()


def test_composition_declares_compaction_in_default_package_set():
    source = (SRC / "app" / "gateway" / "composition.py").read_text(encoding="utf-8")
    assert "ftre.services.compaction" not in source
    assert "ftre.plugins.builtin.compaction" not in source
    assert 'PluginManifest("compaction", "ftre_compaction.plugin:apply", "builtin", False, True' in source
