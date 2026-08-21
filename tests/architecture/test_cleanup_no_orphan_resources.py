"""Architecture gates for resources and model files that must have one Owner."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"


def test_system_prompt_has_one_base_owner() -> None:
    """The application base prompt is loaded from Agent config, not a comment resource."""
    assert not (SRC / "services" / "system_prompt" / "base.md").exists()
    plugin = (SRC / "services" / "system_prompt" / "plugin.py").read_text(encoding="utf-8")
    assert "base.md" not in plugin
    assert "Path(__file__)" not in plugin


def test_removed_orphan_models_have_no_package_files() -> None:
    """Unused placeholder models must not reappear as parallel Owners."""
    assert not (SRC / "services" / "config" / "models.py").exists()
    assert not (SRC / "services" / "session" / "title" / "config.py").exists()
