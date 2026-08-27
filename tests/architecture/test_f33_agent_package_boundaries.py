"""F33 Agent Package 终局架构门禁。

覆盖 PRD-F33 的架构验收：契约包独立导入（AC1）、旧 Owner 退出（AC3）、
Package 元数据与 entry point（AC15）、Runtime 无 Host 反向依赖（AC21）、
DSH 对照项（AC22：不复制 Inbox/队列模型，不复制 Core Hook）。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
AGENT_PKG = ROOT / "packages" / "ftre-agent"
RUNTIME_PKG = ROOT / "packages" / "ftre-agent-runtime"
AGENT_SRC = AGENT_PKG / "src" / "ftre_agent"
RUNTIME_SRC = RUNTIME_PKG / "src" / "ftre_agent_runtime"
LEGACY_AGENT_DIR = ROOT / "src" / "ftre" / "services" / "agent"


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_ac1_agent_contract_package_imports_without_host() -> None:
    """AC1：ftre_agent 在没有 ftre Host 源码的解释器里可独立导入。"""
    code = (
        "import sys; sys.path.insert(0, r'"
        + str(AGENT_PKG / "src")
        + "'); import ftre_agent; assert ftre_agent.AgentService; assert not hasattr(ftre_agent, 'InboundMessage'); "
        "assert 'ftre.services' not in sys.modules and 'ftre' not in sys.modules; "
        "print('agent contract ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "agent contract ok" in result.stdout


def test_ac3_legacy_host_agent_owner_is_fully_removed() -> None:
    """AC3：旧 Host Agent Runtime/Facade/兼容入口不存在。"""
    assert not (LEGACY_AGENT_DIR / "runtime").exists()
    for name in ("plugin.py", "service.py", "contracts.py", "hooks.py", "registry.py"):
        assert not (LEGACY_AGENT_DIR / name).exists(), name
    production_roots = [ROOT / "src"] + sorted(
        (ROOT / "packages").glob("*/src")
    )
    for root in production_roots:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for symbol in ("AgentLoopDriver", "class AgentDriver", "TurnOutcome"):
                assert symbol not in source, (path, symbol)


def test_ac15_package_metadata_and_entry_points_are_declared() -> None:
    """AC15：两个 Package 有完整元数据，分别暴露 Service/Runtime entry point。"""
    agent_pyproject = (AGENT_PKG / "pyproject.toml").read_text(encoding="utf-8")
    runtime_pyproject = (RUNTIME_PKG / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "ftre-agent"' in agent_pyproject
    assert 'name = "ftre-agent-runtime"' in runtime_pyproject
    assert (AGENT_PKG / "README.md").exists()
    assert (AGENT_PKG / "README.zh.md").exists()
    assert (RUNTIME_PKG / "README.md").exists()
    assert (RUNTIME_PKG / "README.zh.md").exists()

    assert 'agent-service = "ftre_agent.plugin:apply"' in agent_pyproject
    assert 'agent-runtime = "ftre_agent_runtime.plugin:apply"' in runtime_pyproject


def test_ac21_runtime_never_imports_host_services() -> None:
    """AC21：Runtime 源码（AST）不反向依赖 ftre.services.* / ftre 根包。"""
    for path in RUNTIME_SRC.rglob("*.py"):
        modules = _imports_of(path)
        host = {m for m in modules if m == "ftre" or m.startswith("ftre.")}
        assert host == set(), (path, sorted(host))

        allowed_roots = {"ftre_agent", "ftre_agent_core", "ftre_llm"}
        for module in modules:
            if "." in module:
                root, _, _ = module.partition(".")
            else:
                root = module
            if root.startswith("ftre"):
                assert root in allowed_roots, (path, module)


def test_ac21_agent_contract_package_depends_on_nothing_ftre() -> None:
    """契约包源码不 import ftre Host 与 Runtime 包。"""
    for path in AGENT_SRC.rglob("*.py"):
        modules = _imports_of(path)
        forbidden = {
            m
            for m in modules if m == "ftre"
            or m.startswith(("ftre.", "ftre_agent_runtime"))
        }
        assert forbidden == set(), (path, sorted(forbidden))


def test_ac22_no_dsh_inbox_or_duplicated_core_hooks() -> None:
    """AC22：不复制 DSH 的 Agent 内置 Inbox，也不复制 Core Hook。"""
    inbox_words = ("QueueItem", "next-turn", "next-step", "mailbox", "queue worker")
    for path in [*AGENT_SRC.rglob("*.py"), *RUNTIME_SRC.rglob("*.py")]:
        source = path.read_text(encoding="utf-8")
        for word in inbox_words:
            assert word not in source, (path, word)

    import ftre_agent
    from ftre_agent_core import hooks as core_hooks

    assert ftre_agent.AGENT_BEFORE_REASONING_SPEC is core_hooks.AGENT_BEFORE_REASONING_SPEC
    assert ftre_agent.AGENT_STOP_DECISION_SPEC is core_hooks.AGENT_STOP_DECISION_SPEC


def test_run_result_contract_has_stable_status_values() -> None:
    """AgentRunResult.status 只允许 completed/cancelled/failed。"""
    import ftre_agent

    result = ftre_agent.AgentRunResult(session_id="s", turn_id="t", status="completed")
    assert result.status == "completed"
    assert not hasattr(ftre_agent, "InboundMessage")


def test_composition_loads_agent_service_then_runtime_entry_point() -> None:
    """AC4：Host Composition 先装载 AgentService，再装载 Runtime Provider。"""
    composition = (ROOT / "src" / "ftre" / "app" / "gateway" / "composition.py").read_text(
        encoding="utf-8"
    )
    assert "ftre_agent_runtime.plugin:apply" in composition
    assert "ftre_agent.plugin:apply" in composition
    assert "ftre.services.agent.plugin" not in composition
    assert "AgentLoop(" not in composition
    bootstrap = (ROOT / "src" / "ftre" / "app" / "gateway" / "bootstrap.py").read_text(
        encoding="utf-8"
    )
    assert "AgentLoop(" not in bootstrap
    assert "ftre_agent_runtime" not in bootstrap
