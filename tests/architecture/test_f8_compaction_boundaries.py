from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"


def test_compaction_implementation_lives_under_service_owner():
    assert (SRC / "services" / "compaction" / "service.py").exists()
    assert not (SRC / "features" / "compaction" / "service.py").exists()
    assert not (SRC / "services" / "compaction" / "contracts.py").exists()
    public_init = (SRC / "services" / "compaction" / "__init__.py").read_text(encoding="utf-8")
    assert "NullCompactionService" not in public_init
    assert not (SRC / "services" / "agent_loop" / "runtime" / "compaction").exists()


def test_agent_loop_and_command_use_compaction_service_directly():
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
        assert "CompactionPort" not in source
        assert "CompactionService" in source
    assert "loop.compaction" not in command


def test_composition_declares_compaction_service_and_hooks():
    source = (SRC / "app" / "gateway" / "composition.py").read_text(encoding="utf-8")
    assert "ftre.services.compaction.plugin:apply" in source
    assert "ftre.features.compaction.plugin:apply" in source
