"""F7 门禁：Agent Runtime 直接消费宿主 Dispatcher，不得回流桥接层。"""

from pathlib import Path

ROOT = Path(__file__).parents[2]
FTRE_SRC = ROOT / "src" / "ftre"
AGENT_CONTRACTS = ROOT / "packages" / "ftre-agent" / "src" / "ftre_agent"


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


def test_agent_hook_contract_has_no_backend_imports():
    source = (AGENT_CONTRACTS / "hooks.py").read_text(encoding="utf-8")
    # 公共 Agent 契约不得反向依赖 ftre Host、Cordis 或具体运行实现。
    assert "import ftre " not in source
    assert "from ftre " not in source
    assert "from ftre." not in source
    assert "import cordis" not in source
    assert "FtreCoreHookManager" not in source
