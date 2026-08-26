"""F6 Hook 边界门禁。"""

from __future__ import annotations

from pathlib import Path

from ftre_agent import (
    AGENT_AFTER_RUN_SPEC,
    AGENT_BEFORE_REASONING_SPEC,
    AGENT_BEFORE_RUN_SPEC,
)

from ftre.services.session.hooks import SESSION_CREATED_SPEC
from ftre.services.system_prompt.hooks import SYSTEM_PROMPT_ASSEMBLE_SPEC
from ftre.services.tools.hooks import TOOL_BEFORE_SPEC

PUBLIC_HOOK_NAMES = {
    AGENT_AFTER_RUN_SPEC.name,
    AGENT_BEFORE_REASONING_SPEC.name,
    AGENT_BEFORE_RUN_SPEC.name,
    SESSION_CREATED_SPEC.name,
    SYSTEM_PROMPT_ASSEMBLE_SPEC.name,
    TOOL_BEFORE_SPEC.name,
}

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"
LEGACY_IMPORT = "ftre.services.agent.runtime.hooks"


def test_public_hook_names_exclude_legacy_filter_names():
    assert "agent/before_messages_build" not in PUBLIC_HOOK_NAMES
    assert "agent/before_run" not in PUBLIC_HOOK_NAMES
    assert "agent/before-run" in PUBLIC_HOOK_NAMES
    assert "agent/before-reasoning" in PUBLIC_HOOK_NAMES
    assert "tool/before" in PUBLIC_HOOK_NAMES
    assert "tool/pre-execute" not in PUBLIC_HOOK_NAMES
    assert "agent/" + "inbox/" + "claimed" not in PUBLIC_HOOK_NAMES
    assert "agent/inbox" not in PUBLIC_HOOK_NAMES
    assert "inbox/" + "claimed" not in PUBLIC_HOOK_NAMES


def test_new_platform_hooks_do_not_import_legacy_runtime_module():
    for path in (SRC / "kernel" / "hooks").rglob("*.py"):
        assert LEGACY_IMPORT not in path.read_text(encoding="utf-8")


def test_legacy_hook_module_and_imports_are_removed():
    assert not (SRC / "services" / "agent" / "runtime" / "hooks.py").exists()
    hits: set[Path] = set()
    for path in SRC.rglob("*.py"):
        if LEGACY_IMPORT in path.read_text(encoding="utf-8"):
            hits.add(path.relative_to(SRC))
    assert not hits


def test_old_filter_contexts_are_absent_from_production_code():
    retired_symbols = (
        "MessagesBuildContext",
        "AgentRunContext",
        "append_to_first_system",
        "agent/before_messages_build",
        "agent/before_run",
    )
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(symbol in text for symbol in retired_symbols), path


def test_features_do_not_import_other_feature_private_modules():
    feature_root = SRC / "plugins" / "builtin"
    feature_names = {
        path.name for path in feature_root.iterdir() if path.is_dir() and path.name != "__pycache__"
    }
    violations: list[str] = []
    for path in feature_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for feature in feature_names:
            if f"ftre.plugins.builtin.{feature}." in text and feature != path.parts[-2]:
                violations.append(f"{path}: ftre.plugins.builtin.{feature}")
    assert not violations, "cross-feature private imports: " + ", ".join(violations)
