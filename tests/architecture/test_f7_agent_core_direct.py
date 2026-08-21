"""F7 门禁：Core 直接消费宿主 Dispatcher，不得回流桥接层。"""

from pathlib import Path

ROOT = Path(__file__).parents[2]
FTRE_SRC = ROOT / "src" / "ftre"
CORE_SRC = Path(r"E:\ftre-agent-core\src\ftre_agent_core")


def _source_files(root: Path):
    return tuple(root.rglob("*.py")) if root.is_dir() else ()


def test_ftre_has_no_core_hook_adapter_owner():
    adapter_dir = FTRE_SRC / "infrastructure" / "agent_core"
    assert not adapter_dir.exists()
    source = "\n".join(path.read_text(encoding="utf-8") for path in _source_files(FTRE_SRC))
    for forbidden in (
        "HookedLLMAdapter",
        "HookedToolRegistry",
        "ToolHookBridge",
        "FtreCoreHookManager",
        "hook_manager",
    ):
        assert forbidden not in source


def test_core_hook_module_has_no_backend_imports():
    source = (CORE_SRC / "hooks.py").read_text(encoding="utf-8")
    assert "import ftre" not in source
    assert "from ftre" not in source
    assert "import cordis" not in source
    assert "FtreCoreHookManager" not in source
