"""F17 Inbox 基础 Owner 门禁。

F17 只验证 Inbox 队列基础设施和 Agent Runtime 边界；业务 Tool 的独立归属由 F18
专门验证，避免把“使用 Inbox”误写成“属于 Inbox”。
"""

from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"
PACKAGE = ROOT / "packages" / "ftre-inbox"


def test_inbox_package_owns_only_queue_runtime() -> None:
    package_src = PACKAGE / "src" / "ftre_inbox"
    assert (package_src / "service.py").is_file()
    assert (package_src / "repository.py").is_file()
    assert (package_src / "hooks.py").is_file()
    plugin = (package_src / "plugin.py").read_text(encoding="utf-8")
    assert 'ctx.provide("inbox", service)' in plugin
    assert "create_send_message_tool" not in plugin
    assert "create_task_tool" not in plugin
    assert "create_team_tools" not in plugin
    assert "ctx.tools.register" not in plugin


def test_business_tool_factories_have_separate_package_owners() -> None:
    package_files = {
        "messaging": ROOT / "packages" / "ftre-messaging" / "src" / "ftre_messaging" / "send_message.py",
        "task": ROOT / "packages" / "ftre-task" / "src" / "ftre_task" / "task.py",
        "team": ROOT / "packages" / "ftre-team" / "src" / "ftre_team" / "team.py",
    }
    assert all(path.is_file() for path in package_files.values())
    for path in package_files.values():
        source = path.read_text(encoding="utf-8")
        assert "ftre.services.tools.builtin" not in source


def test_agent_runtime_does_not_relay_inbox() -> None:
    turn_executor = (SRC / "services" / "agent" / "runtime" / "turn_executor.py").read_text(
        encoding="utf-8"
    )
    # F34：内置工具默认集已随 core-tools Plugin 落位，仍不得包含业务 Tool。
    default_tools = (SRC / "plugins" / "builtin" / "core_tools" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "self._inbox" not in turn_executor
    assert '"inbox":' not in turn_executor
    assert "create_send_message_tool" not in default_tools
    assert "create_task_tool" not in default_tools
    assert "create_team_tools" not in default_tools


def test_inbox_package_does_not_import_host_private_tool_modules() -> None:
    for path in PACKAGE.joinpath("src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "ftre.services.tools.builtin" not in source, path
