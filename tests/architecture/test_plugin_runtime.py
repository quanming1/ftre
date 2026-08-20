from __future__ import annotations

import pytest

from cordis import Context, FiberState
from ftre.platform.plugin_runtime import PluginManager, PluginManifest


@pytest.mark.asyncio
async def test_manager_explicit_external_enablement(tmp_path) -> None:
    module = tmp_path / "external_demo.py"
    module.write_text(
        "def apply(ctx, config=None):\n"
        "    ctx.provide('external_demo', {'enabled': True})\n",
        encoding="utf-8",
    )
    config = {"plugins": [{"id": "external-demo", "entry": "external_demo:apply"}]}
    manager = PluginManager(Context(), plugins_dir=tmp_path)
    statuses = await manager.load([], config)
    assert statuses[0].id == "external-demo"
    assert statuses[0].state is FiberState.ACTIVE
    await manager.close()


@pytest.mark.asyncio
async def test_manifest_dependency_failure_is_observable() -> None:
    manager = PluginManager(Context())

    def consumer(ctx):
        return None

    consumer.inject = ("missing",)
    statuses = await manager.load(
        [PluginManifest("consumer", consumer, required=False)],
        {},
    )
    assert statuses[0].state is FiberState.PENDING
    assert statuses[0].missing == ("missing",)
    await manager.close()

