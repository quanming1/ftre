import pytest
from pydantic import BaseModel, ValidationError

from ftre.plugin.kernel import (
    FtreContext,
    Plugin,
    PluginLoader,
    PluginRegistry,
    PluginState,
)


@pytest.mark.asyncio
async def test_disabled_group_skips_children_and_groups_isolate_services(monkeypatch):
    class First(Plugin):
        name = "first"
        provide = "value"

        async def setup(self, ctx, config):
            ctx.provide("value", "first")

    class Second(Plugin):
        name = "second"
        provide = "value"

        async def setup(self, ctx, config):
            ctx.provide("value", "second")

    class Skipped(Plugin):
        name = "skipped"

        async def setup(self, ctx, config):
            raise AssertionError("disabled group child must not load")

    root = FtreContext()
    loader = PluginLoader(
        root,
        {
            "plugins": [
                {"id": "left", "group": True, "children": [{"name": "first"}]},
                {"id": "right", "group": True, "children": [{"name": "second"}]},
                {
                    "id": "off",
                    "group": True,
                    "disabled": True,
                    "children": [{"name": "skipped"}],
                },
            ]
        },
    )
    monkeypatch.setattr(
        loader,
        "discover",
        lambda: {"first": First, "second": Second, "skipped": Skipped},
    )
    await loader.load()
    assert loader.registry.instances["first"].state is PluginState.ACTIVE
    assert loader.registry.instances["second"].state is PluginState.ACTIVE
    assert "skipped" not in loader.registry.instances
    assert not root.has("value")


@pytest.mark.asyncio
async def test_plugin_config_uses_pydantic_schema():
    class ConfigModel(BaseModel):
        count: int

    class Configured(Plugin):
        name = "configured"
        Config = ConfigModel

        async def setup(self, ctx, config):
            assert config.count == 2

    registry = PluginRegistry(FtreContext())
    await registry.register(Configured, {"count": 2})
    with pytest.raises(ValidationError):
        await registry.register(Configured, {"count": "bad"}, instance_id="bad")


@pytest.mark.asyncio
async def test_legacy_enabled_false_disables_entry(monkeypatch):
    class Disabled(Plugin):
        name = "disabled"

        async def setup(self, ctx, config):
            raise AssertionError("disabled entry must not start")

    loader = PluginLoader(
        FtreContext(),
        {"plugins": [{"name": "disabled", "enabled": False}]},
    )
    monkeypatch.setattr(loader, "discover", lambda: {"disabled": Disabled})
    await loader.load()
    assert loader.registry.instances == {}


@pytest.mark.asyncio
async def test_legacy_stale_module_hint_does_not_block_gateway_startup(
    monkeypatch, caplog
):
    class Discovered(Plugin):
        name = "discovered"

        async def setup(self, ctx, config):
            return None

    loader = PluginLoader(
        FtreContext(),
        {
            "plugins": [
                {"name": "cron", "module": "cron_plugin.CronPlugin"},
                {
                    "name": "discovered",
                    "module": "removed_module.RemovedPlugin",
                },
            ]
        },
    )
    monkeypatch.setattr(loader, "discover", lambda: {"discovered": Discovered})

    await loader.load()

    assert "cron" not in loader.registry.instances
    assert loader.registry.instances["discovered"].state is PluginState.ACTIVE
    assert "configured entry unavailable, skipped: id=cron" in caplog.text
