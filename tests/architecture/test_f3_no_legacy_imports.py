"""F3 gates preventing the retired Plugin Kernel/API from returning."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "src"


def _python_files(root: Path):
    return root.rglob("*.py") if root.exists() else ()


def test_retired_kernel_builtin_and_aggregate_api_have_no_python_modules() -> None:
    retired = (
        SOURCE / "ftre" / "plugin" / "kernel",
        SOURCE / "ftre" / "plugin" / "builtin",
        SOURCE / "ftre" / "api",
    )
    assert all(not tuple(_python_files(path)) for path in retired)
    assert not (SOURCE / "ftre" / "plugin" / "api.py").exists()


def test_runtime_source_does_not_import_retired_namespaces() -> None:
    forbidden = ("ftre.plugin", "ftre.api")
    roots = (SOURCE / "cordis", SOURCE / "ftre")
    for root in roots:
        for path in _python_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                assert not any(name == forbidden_name or name.startswith(forbidden_name + ".") for name in names for forbidden_name in forbidden), path


def test_new_hook_contract_is_the_single_runtime_hook_owner() -> None:
    hooks = SOURCE / "ftre" / "services" / "agent" / "runtime" / "hooks.py"
    assert hooks.is_file()
    assert "class AgentRunContext" in hooks.read_text(encoding="utf-8")
    assert "class MessagesBuildContext" in hooks.read_text(encoding="utf-8")
