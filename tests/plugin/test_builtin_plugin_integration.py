import asyncio

import pytest
from ftre_agent_core.hooks import FtreCoreHookManager
from ftre_agent_core.tool import ToolRegistry

from ftre.command import CommandManager
from ftre.plugin import FtreContext, PluginLoader, PluginState, install_core_services


@pytest.mark.asyncio
async def test_all_builtin_plugins_activate_on_kernel(monkeypatch, tmp_path):
    from ftre.plugin.builtin import mcp_plugin

    monkeypatch.setattr(mcp_plugin, "_read_config_json", dict)
    context = FtreContext()
    install_core_services(
        context,
        bus=object(),
        channel_manager=object(),
        session_manager=object(),
        core_hook_manager=FtreCoreHookManager(),
        tool_registry=ToolRegistry(),
        event_loop=lambda: asyncio.get_running_loop(),
        command_manager=CommandManager(),
        routers=[],
    )
    loader = PluginLoader(
        context,
        {"plugins": [{"name": "skill", "config": {"skills_dir": str(tmp_path)}}]},
        plugins_dir=tmp_path / "no-external-plugins",
    )

    await loader.load()

    expected = {"context_govern", "mcp", "plan", "skill", "team", "title_gen"}
    assert expected.issubset(loader.registry.instances)
    assert all(
        loader.registry.instances[name].state is PluginState.ACTIVE for name in expected
    )
    assert "loadSkill" in context.get("tool_registry").names
    assert context.get("routers")

    await loader.close()
    assert context.get("tool_registry").names == []
    assert context.get("routers") == []
