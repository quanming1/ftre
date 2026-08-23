"""F14.7 Package 发行边界门禁。

这些断言只检查可自动验证的发行事实：Host extras 不变成硬依赖，两个可选
发行物都有唯一 entry point，源码不反向拿 Host 私有 Runtime/Repository，且
仓库中不能把缓存或构建产物打进 wheel。真正的 wheel 构建和洁净 venv 安装
在执行报告中运行，因为它们需要隔离的临时环境。
"""

from __future__ import annotations

import ast
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[2]
PACKAGES = ROOT / "packages"


def _project(path: Path) -> dict:
    return tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_host_extras_are_optional_package_edges() -> None:
    """Host 只声明可选安装组合，不在源码或依赖中硬编码两个 Package。"""
    project = _project(ROOT)
    extras = project["optional-dependencies"]
    assert extras["inbox"] == ["ftre-inbox>=0.1.0"]
    assert extras["compaction"] == ["ftre-compaction>=0.1.0"]
    assert extras["full"] == ["ftre-inbox>=0.1.0", "ftre-compaction>=0.1.0"]
    assert all("ftre-inbox" not in item and "ftre-compaction" not in item for item in project["dependencies"])


def test_optional_packages_have_one_entry_point_and_metadata() -> None:
    """每个发行物都能被 Plugin discovery 识别，且版本/README/build 元数据完整。"""
    expected = {
        "ftre-inbox": ("inbox", "ftre_inbox.plugin:apply"),
        "ftre-compaction": ("compaction", "ftre_compaction.plugin:apply"),
    }
    for package_name, (plugin_id, entry) in expected.items():
        package = PACKAGES / package_name
        project = _project(package)
        assert project["name"] == package_name
        assert project["version"]
        assert (package / "README.md").is_file()
        points = project["entry-points"]["ftre.plugins"]
        assert points == {plugin_id: entry}
        assert "build-backend" in tomllib.loads(
            (package / "pyproject.toml").read_text(encoding="utf-8")
        )["build-system"]


def test_plugin_discovery_reads_installed_entry_points_without_importing(monkeypatch) -> None:
    """安装包只进入候选目录；未启用时不得执行 entry point 模块。"""
    from ftre.kernel.plugins.discovery import PluginDiscovery

    class EntryPoints(list):
        def select(self, *, group):
            assert group == "ftre.plugins"
            return self

    fake = EntryPoints(
        [
            SimpleNamespace(
                name="sample-package",
                value="sample_package.plugin:apply",
                dist=SimpleNamespace(version="1.2.3", metadata={"Summary": "sample"}),
            )
        ]
    )
    monkeypatch.setattr("importlib.metadata.entry_points", lambda: fake)
    catalog = PluginDiscovery(plugins_dir=ROOT / "does-not-exist").catalog([])
    manifest = catalog.require("sample-package")
    assert manifest.entry == "sample_package.plugin:apply"
    assert manifest.source == "external:sample-package"
    assert manifest.default_enabled is False


def test_package_sources_use_only_public_host_boundaries() -> None:
    """Package 不得 import Host 的 Runtime、Repository 或 Composition 私有实现。"""
    forbidden = (
        "ftre.services.agent.runtime",
        "ftre.services.session.persistence",
        "ftre.app.gateway",
        "ftre.kernel.plugins.loader",
    )
    for path in PACKAGES.glob("*/src/**/*.py"):
        source = path.read_text(encoding="utf-8")
        assert not any(item in source for item in forbidden), path

        # 解析 import 而不是只做字符串匹配，避免注释里的路径改变门禁结果。
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            assert not any(
                name.startswith(forbidden_item) for name in names for forbidden_item in forbidden
            ), path


def test_package_source_tree_has_no_generated_artifacts() -> None:
    """源码发行目录不得携带 pycache、字节码、build/dist 或测试数据库。"""
    forbidden_names = {"__pycache__", "build", "dist", ".pytest_cache", ".ruff_cache"}
    for package in PACKAGES.iterdir():
        if not package.is_dir():
            continue
        # 测试运行本身会生成 __pycache__；这里读取 Git 已跟踪的发行输入，
        # 既能拦截误提交的生成物，又不会把测试副作用当成源码债务。
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z", "--", str(package / "src")], cwd=ROOT
        ).split(b"\0")
        for raw in tracked:
            if not raw:
                continue
            path = ROOT / raw.decode()
            assert path.name not in forbidden_names, path
            assert path.suffix not in {".pyc", ".pyo", ".sqlite", ".db"}, path
