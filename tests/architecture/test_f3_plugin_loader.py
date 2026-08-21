"""Composition-facing PluginManager contract replacing the old Kernel loader."""

from __future__ import annotations

import pytest
from cordis import Context, FiberState

from ftre.platform.plugin_runtime import PluginDiscovery, PluginManager, PluginManifest


@pytest.mark.asyncio
async def test_required_plugin_failure_is_reported_and_context_is_cleaned() -> None:
    context = Context()
    manager = PluginManager(context)

    def broken(_ctx):
        raise RuntimeError("broken entry")

    from ftre.platform.plugin_runtime import PluginStartupError

    with pytest.raises(PluginStartupError):
        await manager.load([PluginManifest("required-broken", broken, required=True)], {})
    assert not context.reflect.store


@pytest.mark.asyncio
async def test_manifest_entry_can_provide_a_service() -> None:
    context = Context()
    manager = PluginManager(context)

    def apply(ctx, _config=None):
        ctx.provide("loaded", True)

    statuses = await manager.load([PluginManifest("loaded", apply)], {})
    assert statuses[0].state is FiberState.ACTIVE
    assert context.get("loaded") is True
    await manager.close()


def test_plugin_entry_requires_module_attribute_contract() -> None:
    discovery = PluginDiscovery()
    with pytest.raises(ValueError, match="module:attribute"):
        discovery.resolve(PluginManifest("legacy-entry", "legacy_module.LegacyPlugin"))


def test_plugin_manifest_rejects_legacy_module_config_key() -> None:
    discovery = PluginDiscovery()
    with pytest.raises(ValueError, match="requires entry"):
        discovery.catalog([], {"plugins": [{"id": "legacy-config", "module": "legacy:apply"}]})


def test_disabled_external_plugin_does_not_require_entry() -> None:
    discovery = PluginDiscovery()
    catalog = discovery.catalog(
        [],
        {"plugins": [{"id": "legacy-config", "enabled": False}]},
    )
    assert catalog.get("legacy-config") is None
