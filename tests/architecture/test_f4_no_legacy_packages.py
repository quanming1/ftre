"""F4.2 gates for retired data-plane import surfaces."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "src"


def _python_files(root: Path):
    return root.rglob("*.py") if root.exists() else ()


def test_data_plane_legacy_packages_have_no_python_modules() -> None:
    legacy_roots = (
        SOURCE / "ftre" / "agent",
        SOURCE / "ftre" / "session",
        SOURCE / "ftre" / "bus",
        SOURCE / "ftre" / "channel",
        SOURCE / "ftre" / "command",
        SOURCE / "ftre" / "tools",
    )
    assert all(not tuple(_python_files(path)) for path in legacy_roots)


def test_root_owners_are_removed_after_migration() -> None:
    assert not (SOURCE / "ftre" / "config.py").exists()
    assert not (SOURCE / "ftre" / "trace_store.py").exists()
    assert not tuple(_python_files(SOURCE / "ftre" / "mcp"))
    assert not tuple(_python_files(SOURCE / "ftre" / "gateway"))
    assert not tuple(_python_files(SOURCE / "ftre" / "utils"))
    assert (SOURCE / "ftre" / "app" / "gateway" / "process.py").is_file()
    assert not (SOURCE / "ftre" / "services" / "attachment" / "store.py").exists()
    assert (SOURCE / "ftre" / "services" / "attachment" / "codec.py").is_file()


def test_new_layers_do_not_import_retired_data_plane_namespaces() -> None:
    forbidden = ("ftre.agent", "ftre.session", "ftre.bus", "ftre.channel", "ftre.command", "ftre.tools")
    roots = (SOURCE / "ftre" / "app", SOURCE / "ftre" / "kernel", SOURCE / "ftre" / "services", SOURCE / "ftre" / "plugins")
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
                assert not any(name == prefix or name.startswith(prefix + ".") for name in names for prefix in forbidden), path


def test_new_layers_do_not_use_module_identity_replacement() -> None:
    for root in (SOURCE / "ftre" / "app", SOURCE / "ftre" / "kernel", SOURCE / "ftre" / "services", SOURCE / "ftre" / "plugins"):
        for path in _python_files(root):
            source = path.read_text(encoding="utf-8")
            assert "sys.modules[__name__]" not in source, path


def test_production_tree_has_no_wildcard_reexports_or_dead_http_compat_api() -> None:
    for path in _python_files(SOURCE / "ftre"):
        source = path.read_text(encoding="utf-8")
        assert "import *" not in source, path
        assert "register_compat_" not in source, path
        assert "ApiDependencies" not in source, path
