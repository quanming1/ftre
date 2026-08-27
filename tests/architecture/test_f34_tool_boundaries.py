"""F34 架构门禁：ToolService 唯一 Owner 与 Runtime 工具边界。

- registry 私有化后，src/tests 不得再引用公有 ``ToolService.registry``；
- ``services/tools/builtin/`` 目录删除，内置工具随 core-tools Plugin 落位；
- Agent Runtime 不构造 ToolRegistry、不 import builtin 工具模块；
- Composition 清单包含 core-tools（必选）与 tool-audit（可选）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "ftre"
TESTS = ROOT / "tests"


def _python_files(root: Path):
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_services_tools_builtin_directory_is_removed() -> None:
    """旧 builtin 目录删除；过滤逻辑由 services/tools/filtering.py 拥有。"""
    assert not (SRC / "services" / "tools" / "builtin").exists()
    assert (SRC / "services" / "tools" / "filtering.py").is_file()
    assert (SRC / "plugins" / "builtin" / "core_tools" / "plugin.py").is_file()
    assert (SRC / "plugins" / "builtin" / "tool_audit" / "plugin.py").is_file()


def test_no_public_registry_attribute_references_remain() -> None:
    """源文本不得再出现公有 registry 访问（self.registry 定义在 service 内已私有化）。"""
    forbidden = ("tools.registry", "tool_service.registry", "_tools.registry")
    for path in _python_files(SRC):
        source = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source, (path, marker)
    # 测试侧同样不得回退到直接摸 registry 的旧模式。
    for path in _python_files(TESTS):
        source = path.read_text(encoding="utf-8")
        if path.name == Path(__file__).name:
            continue
        for marker in ("tools.registry", "tool_service.registry"):
            assert marker not in source, (path, marker)


def test_agent_runtime_does_not_construct_tool_registry_or_import_builtin() -> None:
    """Runtime 只消费 ToolService.prepare_view，不构造 Registry、不 import 工具模块。

    注意：``AgentService.registry``（AgentRegistry）是 F32 登记的另一项独立债务，
    不在 F34 范围；这里只约束 Tool 领域的边界。
    """
    runtime_root = (
        Path(__file__).parents[2]
        / "packages"
        / "ftre-agent-runtime"
        / "src"
        / "ftre_agent_runtime"
    )
    for path in _python_files(runtime_root):
        source = path.read_text(encoding="utf-8")
        assert "ToolRegistry(" not in source, path
        assert "from ftre_agent_core.tool import" not in source, path
        assert "ftre.services.tools.builtin" not in source, path
        assert "ftre.plugins.builtin.core_tools" not in source, path
        assert "tools.registry" not in source, path


def test_core_tools_plugin_registers_via_public_service_only() -> None:
    """core-tools 只通过 register 贡献，不构造全局 registry、不走 prepare_view 特例。"""
    source = (SRC / "plugins" / "builtin" / "core_tools" / "plugin.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            assert not (
                isinstance(func, ast.Attribute) and func.attr == "register_view_preparer"
            ), "core-tools 不应使用 view preparer（工具是普通贡献）"
    assert 'ctx.tools.register' in source
    assert 'owner="core-tools"' in source
    # service.py 不得再硬编码内置工具工厂（F34 删除重复路径）。
    service_source = (SRC / "services" / "tools" / "service.py").read_text(
        encoding="utf-8"
    )
    for marker in ("create_bash_tool", "create_read_tool", "create_write_tool",
                   "create_edit_tool", "create_set_workspace_tool"):
        assert marker not in service_source, marker


def test_view_preparer_contract_is_general_not_mcp_specific() -> None:
    """preparer 契约是通用四参签名；service 不得再提取 mcp_config 特例。"""
    service_source = (SRC / "services" / "tools" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "mcp_config" not in service_source
    # MCP Plugin 自己从 profile 片段读取 mcp_config，不再由 Service 预提取。
    mcp_source = (SRC / "plugins" / "builtin" / "mcp" / "plugin.py").read_text(
        encoding="utf-8"
    )
    assert "mcp_config" in mcp_source


def test_composition_manifests_include_core_tools_and_tool_audit() -> None:
    """默认清单包含 core-tools（必选）与 tool-audit（可选观察能力）。"""
    source = (ROOT / "src" / "ftre" / "app" / "gateway" / "composition.py").read_text(
        encoding="utf-8"
    )
    assert 'PluginManifest("core-tools", "ftre.plugins.builtin.core_tools.plugin:apply", "builtin", True' in source
    assert 'PluginManifest("tool-audit", "ftre.plugins.builtin.tool_audit.plugin:apply", "builtin", False' in source


def test_business_packages_still_do_not_import_builtin_tools() -> None:
    """仓内业务包不得 import Host 工具模块（保持 F18 门禁语义）。"""
    for directory in ("ftre-inbox", "ftre-compaction", "ftre-messaging", "ftre-task", "ftre-team"):
        source_root = ROOT / "packages" / directory / "src"
        if not source_root.exists():
            continue
        for path in _python_files(source_root):
            source = path.read_text(encoding="utf-8")
            assert "ftre.services.tools.builtin" not in source, path
            assert "ftre.plugins.builtin.core_tools" not in source, path
