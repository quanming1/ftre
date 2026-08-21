from __future__ import annotations

import pytest
from cordis import Context, FiberState

from ftre.platform.plugin_runtime import (
    PluginManager,
    PluginManifest,
    PluginStartupError,
)


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


@pytest.mark.asyncio
async def test_external_candidate_is_not_imported_until_enabled(tmp_path) -> None:
    marker = tmp_path / "imported.txt"
    (tmp_path / "candidate.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
        "def apply(ctx, config=None):\n    return None\n",
        encoding="utf-8",
    )
    disabled = PluginManager(Context(), plugins_dir=tmp_path)
    await disabled.load([], {})
    assert not marker.exists()
    await disabled.close()

    enabled = PluginManager(Context(), plugins_dir=tmp_path)
    statuses = await enabled.load([], {"plugins": [{"id": "candidate", "entry": "candidate:apply"}]})
    assert statuses[0].state is FiberState.ACTIVE
    assert marker.read_text(encoding="utf-8") == "imported"
    await enabled.close()


@pytest.mark.asyncio
async def test_required_entry_failure_is_fail_loud_and_disposes_context() -> None:
    ctx = Context()

    def broken(_ctx):
        raise RuntimeError("broken entry")

    manager = PluginManager(ctx)
    with pytest.raises(PluginStartupError):
        await manager.load([PluginManifest("required-broken", broken, required=True)], {})
    assert not ctx.reflect.store
