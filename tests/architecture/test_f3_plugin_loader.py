"""Composition-facing PluginManager contract replacing the old Kernel loader."""

from __future__ import annotations

import pytest

from cordis import Context, FiberState
from ftre.platform.plugin_runtime import PluginManager, PluginManifest


@pytest.mark.asyncio
async def test_required_plugin_failure_is_reported_and_context_is_cleaned() -> None:
    context = Context()
    manager = PluginManager(context)

    def broken(_ctx):
        raise RuntimeError("broken entry")

    from ftre.platform.plugin_runtime import PluginStartupError

    with pytest.raises(PluginStartupError):
        await manager.load([PluginManifest("required-broken", broken, required=True)], {})
    assert context.services == {}


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
