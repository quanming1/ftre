from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "ftre"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module)
    return values


def test_new_layers_do_not_import_private_kernel_or_upward_layers() -> None:
    new_layers = [ROOT / "app", ROOT / "kernel", ROOT / "services", ROOT / "plugins"]
    for layer in new_layers:
        for path in layer.rglob("*.py"):
            imports = _imports(path)
            assert not any("ftre.plugin.kernel" in item for item in imports), path
            if layer.name == "kernel":
                assert not any(item.startswith("ftre.plugins.builtin") for item in imports), path
            if layer.name == "services":
                assert not any(item.startswith(("ftre.plugins.builtin", "ftre.app")) for item in imports), path


def test_cli_is_only_a_forwarder() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    for constructor in ("SessionManager(", "AgentLoop(", "ChannelManager(", "CronScheduler(", "PluginManager("):
        assert constructor not in source
