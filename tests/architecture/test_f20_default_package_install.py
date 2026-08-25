"""F20 默认发行组合门禁。

这里同时检查“安装默认包含”和“装配默认启用”两层事实，避免只修改根依赖却忘记
Composition，或只增加 Manifest 却让干净安装缺少 entry point。测试不连接网络，也不
执行真实 LLM；Package 的业务行为由各自包测试覆盖。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ftre.app.gateway.composition import default_manifests

ROOT = Path(__file__).parents[2]
PACKAGES = ROOT / "packages"


def test_all_workspace_packages_are_default_dependencies() -> None:
    """根发行物必须声明 packages 下每个 pyproject 的发行名称。"""
    root_project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = set(root_project["dependencies"])
    expected = {
        "ftre-compaction>=0.2.0,<0.3.0",
        "ftre-inbox>=0.2.0,<0.3.0",
        "ftre-messaging>=0.1.0,<0.2.0",
        "ftre-task>=0.1.0,<0.2.0",
        "ftre-team>=0.1.0,<0.2.0",
        "ftre-llm-recovery>=0.1.0,<0.2.0",
        "ftre-llm-fallback>=0.1.0,<0.2.0",
    }
    assert expected <= dependencies


def test_each_workspace_package_declares_unique_plugin_entry() -> None:
    """每个独立发行物只贡献一个稳定的 ftre.plugins entry point。"""
    expected = {
        "ftre-compaction": ("compaction", "ftre_compaction.plugin:apply"),
        "ftre-inbox": ("inbox", "ftre_inbox.plugin:apply"),
        "ftre-messaging": ("messaging", "ftre_messaging.plugin:apply"),
        "ftre-task": ("task", "ftre_task.plugin:apply"),
        "ftre-team": ("team", "ftre_team.plugin:apply"),
        "ftre-llm-recovery": ("llm-recovery", "ftre_llm_recovery.plugin:apply"),
        "ftre-llm-fallback": ("llm-fallback", "ftre_llm_fallback.plugin:apply"),
    }
    for package_name, (plugin_id, entry) in expected.items():
        project = tomllib.loads(
            (PACKAGES / package_name / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        assert project["entry-points"]["ftre.plugins"] == {plugin_id: entry}


def test_default_composition_declares_all_workspace_package_plugins() -> None:
    """默认装配清单必须与默认发行组合保持一一对应。"""
    manifests = {item.id: item for item in default_manifests()}
    expected = {
        "compaction": "ftre_compaction.plugin:apply",
        "inbox": "ftre_inbox.plugin:apply",
        "messaging": "ftre_messaging.plugin:apply",
        "task": "ftre_task.plugin:apply",
        "team": "ftre_team.plugin:apply",
        "llm-recovery": "ftre_llm_recovery.plugin:apply",
        "llm-fallback": "ftre_llm_fallback.plugin:apply",
    }
    assert {item: manifests[item].entry for item in expected} == expected
    assert all(manifests[item].default_enabled for item in expected)
