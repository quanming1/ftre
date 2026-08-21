from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"


def test_compaction_implementation_lives_under_feature_owner():
    assert (SRC / "features" / "compaction" / "service.py").exists()
    assert not (SRC / "services" / "agent_loop" / "runtime" / "compaction").exists()


def test_agent_loop_and_command_use_public_compaction_port_only():
    engine = (SRC / "services" / "agent_loop" / "runtime" / "loop" / "engine.py").read_text(
        encoding="utf-8"
    )
    gate = (SRC / "services" / "agent_loop" / "runtime" / "loop" / "context_gate.py").read_text(
        encoding="utf-8"
    )
    command = (SRC / "services" / "command" / "builtin.py").read_text(encoding="utf-8")
    for source in (engine, gate, command):
        assert "CompactManager" not in source
        assert "services.agent_loop.runtime.compaction" not in source
    assert "CompactionPort" in gate
    assert "loop.compaction.compact_now" in command


def test_composition_declares_compaction_feature():
    source = (SRC / "app" / "gateway" / "composition.py").read_text(encoding="utf-8")
    assert "ftre.features.compaction.plugin:apply" in source
