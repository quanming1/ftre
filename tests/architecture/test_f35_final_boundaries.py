"""F35.6 终局边界与旧输入符号门禁。"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
AGENT_SRC = ROOT / "packages" / "ftre-agent" / "src" / "ftre_agent"
RUNTIME_SRC = ROOT / "packages" / "ftre-agent-runtime" / "src" / "ftre_agent_runtime"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_agent_contract_has_no_inbox_or_legacy_input_symbols() -> None:
    forbidden = {"InboundMessage", "QueueItem", "Channel", "Repository", "create_llm_handler"}
    for path in AGENT_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
        assert not forbidden.intersection(names), (path, forbidden.intersection(names))
        source = path.read_text(encoding="utf-8")
        assert "InboundMessage" not in source
        assert "QueueItem" not in source


def test_runtime_uses_internal_runtime_input_and_no_host_imports() -> None:
    assert (RUNTIME_SRC / "protocol.py").is_file()
    for path in RUNTIME_SRC.rglob("*.py"):
        imports = _imports(path)
        assert not any(module == "ftre" or module.startswith("ftre.") for module in imports), path
        source = path.read_text(encoding="utf-8")
        assert "InboundMessage" not in source


def test_legacy_host_agent_source_tree_has_no_python_owner() -> None:
    legacy = ROOT / "src" / "ftre" / "services" / "agent"
    assert not list(legacy.rglob("*.py"))
