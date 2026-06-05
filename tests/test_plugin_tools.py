import pytest
from ftre_agent_core.tool import Tool

from ftre.plugin import HookManager, Plugin, PluginManager
from ftre.tools import ToolRegistry, build_default_tools


def _dummy_tool(name: str = "dummy") -> Tool:
    def dummy() -> str:
        return "ok"

    return Tool(
        name=name,
        description="dummy tool",
        parameters=[],
        func=dummy,
    )


def test_tool_registry_rejects_duplicate_names():
    registry = ToolRegistry()
    registry.register(_dummy_tool("dup"))

    with pytest.raises(ValueError, match="tool already registered"):
        registry.register(_dummy_tool("dup"))


def test_build_default_tools_includes_registry_tools():
    registry = ToolRegistry()
    registry.register(_dummy_tool("extra"))

    names = [tool.name for tool in build_default_tools(tool_registry=registry)]

    assert "extra" in names


def test_plugin_manager_rolls_back_tools_when_setup_fails():
    class FailingToolPlugin(Plugin):
        name = "failing_tool"

        def setup(self) -> None:
            self.api.register_tool(_dummy_tool("leaked"))
            raise RuntimeError("boom")

    registry = ToolRegistry()
    manager = PluginManager(
        bus=None,
        channel_manager=None,
        session_manager=None,
        hook_manager=HookManager(),
        tool_registry=registry,
    )

    manager._load(FailingToolPlugin(), {})

    assert "leaked" not in [tool.name for tool in registry.snapshot()]


def test_plugin_api_register_tool_adds_to_shared_registry():
    class ToolPlugin(Plugin):
        name = "tool_plugin"

        def setup(self) -> None:
            self.api.register_tool(_dummy_tool("from_plugin"))

    registry = ToolRegistry()
    manager = PluginManager(
        bus=None,
        channel_manager=None,
        session_manager=None,
        hook_manager=HookManager(),
        tool_registry=registry,
    )

    manager._load(ToolPlugin(), {})

    assert [tool.name for tool in registry.snapshot()] == ["from_plugin"]


