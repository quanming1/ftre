"""F18 Tool Package 边界门禁。

这些断言保护“依赖关系不等于业务 Owner”：Inbox 只拥有队列运行时，三个业务 Package
分别拥有自己的 Tool 注册和生命周期。
"""

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_three_business_packages_have_independent_entrypoints() -> None:
    expected = {
        "ftre-messaging": ("ftre_messaging.plugin:apply", "messaging"),
        "ftre-task": ("ftre_task.plugin:apply", "task"),
        "ftre-team": ("ftre_team.plugin:apply", "team"),
    }
    for directory, (entry, plugin_id) in expected.items():
        project = ROOT / "packages" / directory
        pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
        assert f'name = "{directory}"' in pyproject
        assert f'{plugin_id} = "{entry}"' in pyproject
        assert (project / "README.md").is_file()


def test_tool_owner_is_not_inbox() -> None:
    inbox_plugin = (ROOT / "packages/ftre-inbox/src/ftre_inbox/plugin.py").read_text(
        encoding="utf-8"
    )
    assert "ctx.tools.register" not in inbox_plugin
    assert "create_send_message_tool" not in inbox_plugin
    assert "create_task_tool" not in inbox_plugin
    assert "create_team_tools" not in inbox_plugin

    plugin_owner_pairs = {
        "ftre-messaging/src/ftre_messaging/plugin.py": 'owner="messaging"',
        "ftre-task/src/ftre_task/plugin.py": 'owner="task"',
        "ftre-team/src/ftre_team/plugin.py": 'owner="team"',
    }
    for relative, marker in plugin_owner_pairs.items():
        source = (ROOT / "packages" / relative).read_text(encoding="utf-8")
        assert "ctx.tools.register" in source
        assert marker in source


def test_old_duplicate_team_provider_is_removed() -> None:
    old_team = ROOT / "src/ftre/plugins/builtin/team"
    assert not old_team.exists()
    composition = (ROOT / "src/ftre/app/gateway/composition.py").read_text(encoding="utf-8")
    assert '"ftre.plugins.builtin.team.plugin:apply"' not in composition


def test_business_packages_do_not_import_private_host_owners() -> None:
    for directory in ("ftre-messaging", "ftre-task", "ftre-team"):
        source_root = ROOT / "packages" / directory / "src"
        for path in source_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "ftre.services.tools.builtin" not in source, path
            assert "ftre.services.agent.runtime" not in source, path
            assert "ftre.services.agent.profile import sub_agent" not in source, path
