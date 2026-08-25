"""F16 Hook 面终局基线与目标门禁。

该测试从实际导出的 ``HookSpec`` 读取事实，而不是复制生产表格。C5 增加 llm/error 后，
全系统 16 个名称仍必须唯一，门禁会阻止旧 Hook 以 alias 或第二份 Spec 偷渡回来。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest

CORE_MODULE = "ftre_agent_core.hooks"
HOST_MODULES = (
    "ftre.services.agent.hooks",
    "ftre.services.session.hooks",
    "ftre.services.messaging.bus.ingress",
    "ftre.services.system_prompt.hooks",
)
PACKAGE_MODULES = ("ftre_inbox.hooks",)

# F16 完成后的目标快照；Core 5 项与 Host/Package 10 项均必须唯一。
CURRENT_HOOK_NAMES = {
    "tool/before",
    "tool/after",
    "llm/stream",
    "llm/error",
    "agent/before-reasoning",
    "agent/stop-decision",
    "agent/before-run",
    "agent/after-run",
    "agent/run-error",
    "session/created",
    "session/disposed",
    "messaging/route",
    "system-prompt/assemble",
    "inbox/before-claim",
    "inbox/changed",
    "inbox/status-changed",
}

F16_TARGET_HOOK_NAMES = {
    "tool/before",
    "tool/after",
    "llm/stream",
    "llm/error",
    "agent/before-reasoning",
    "agent/stop-decision",
    "agent/before-run",
    "agent/after-run",
    "agent/run-error",
    "session/created",
    "session/disposed",
    "messaging/route",
    "system-prompt/assemble",
    "inbox/before-claim",
    "inbox/changed",
    "inbox/status-changed",
}


def _specs_from_module(module_name: str) -> dict[str, Any]:
    """读取模块公开的 Spec；忽略类型、常量和业务函数，避免手工重复清单。"""

    module = importlib.import_module(module_name)
    return {
        name: value
        for name, value in vars(module).items()
        if name.endswith("_SPEC") and hasattr(value, "name") and hasattr(value, "domain")
    }


def _fact_snapshot() -> dict[str, list[tuple[str, str, str, str]]]:
    snapshot: dict[str, list[tuple[str, str, str, str]]] = {
        "core": [],
        "host": [],
        "package": [],
    }
    for group, modules in (
        ("core", (CORE_MODULE,)),
        ("host", HOST_MODULES),
        ("package", PACKAGE_MODULES),
    ):
        for module_name in modules:
            for spec in _specs_from_module(module_name).values():
                snapshot[group].append(
                    (spec.name, module_name, spec.mode.value, spec.scope.value)
                )
    return snapshot


def test_f16_target_snapshot_has_exactly_16_unique_hook_names():
    snapshot = _fact_snapshot()
    names = [item[0] for group in snapshot.values() for item in group]
    # Agent Host 为了稳定导入面重导出两项 Core Spec；事实门禁按唯一名称计数，
    # 不把同一个对象的公开重导出误判为第二个 Hook Owner。
    assert len(set(names)) == 16
    assert set(names) == CURRENT_HOOK_NAMES


def test_f16_target_set_is_explicit_and_core_boundary_is_frozen():
    assert len(F16_TARGET_HOOK_NAMES) == 16
    core_names = {
        "tool/before",
        "tool/after",
        "llm/stream",
        "llm/error",
        "agent/before-reasoning",
        "agent/stop-decision",
    }
    assert core_names <= F16_TARGET_HOOK_NAMES
    assert len(F16_TARGET_HOOK_NAMES - core_names) == 10


@pytest.mark.parametrize("name", sorted(CURRENT_HOOK_NAMES))
def test_baseline_hook_names_have_one_domain_separator(name: str):
    domain, separator, local_name = name.partition("/")
    assert separator == "/", name
    assert domain and local_name and "/" not in local_name


def test_f15_target_does_not_reintroduce_unscoped_business_terms():
    forbidden = {
        "agent/" + "request",
        "session/" + "event",
        "session/" + "status",
        "messaging/" + "inbound",
    }
    assert forbidden.isdisjoint(F16_TARGET_HOOK_NAMES)


def test_production_hook_registration_uses_context_and_single_runtime_owner():
    """Hook Runtime 的 Context/Effect 规则由源码门禁保护，避免只靠代码审查。"""

    root = Path(__file__).parents[2]
    production_roots = (root / "src", root / "packages")
    for source_root in production_roots:
        for path in source_root.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            assert "global_" + "listener" not in text, path
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "register":
                    continue
                receiver = ast.unparse(node.func.value)
                if "hook_runtime" not in receiver:
                    continue
                keyword_names = {keyword.arg for keyword in node.keywords if keyword.arg}
                assert "context" in keyword_names, path
                assert "all_agent_scopes" in keyword_names, path
            assert "receipt." + "dispose" not in text, path


def test_retired_host_hook_names_are_absent_from_production_sources():
    """删除的时机不能只从导出表消失，生产源码也不得继续发布或引用它们。"""

    root = Path(__file__).parents[2]
    retired = (
        "agent/" + "before-turn",
        "agent/" + "after-turn",
        "agent/" + "request",
        "agent/" + "request-error",
        "agent/" + "turn-stopped",
        "agent/" + "created",
        "agent/" + "disposed",
        "agent/" + "error",
        "agent/" + "session-start",
        "agent/" + "status",
        "session/" + "event",
        "session/" + "flush",
        "messaging/" + "inbound",
        "inbox/" + "inserted",
        "inbox/" + "claimed",
        "inbox/" + "discarded",
        "global_" + "listener",
    )
    for source_root in (root / "src", root / "packages"):
        for path in source_root.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            assert not any(name in text for name in retired), path
