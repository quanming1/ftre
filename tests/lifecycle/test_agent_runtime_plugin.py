"""F13 Agent runtime Provider Plugin lifecycle coverage."""

import pytest

from ftre.app.gateway.composition import build_composition


@pytest.mark.asyncio
async def test_agent_service_owns_private_runtime_and_detaches_on_close(tmp_path) -> None:
    composition = await build_composition({"sessions_dir": str(tmp_path / "sessions")})
    agents = composition.context.get("agents")
    assert agents is not None
    assert agents.driver is not None
    assert composition.context.get("agent_runtime", strict=False) is None
    assert composition.plugins.loader._manifests.get("agent-runtime") is None
    # Channel providers are composed by their own Plugins; the Gateway does
    # not need to instantiate or register protocol implementations manually.
    channels = composition.context.get("channels")
    assert channels.manager.get("ws") is not None
    assert channels.manager.get("subagent") is not None

    await composition.close()

    with pytest.raises(RuntimeError, match="runtime is not ready"):
        _ = agents.driver


@pytest.mark.asyncio
async def test_gateway_endpoint_override_is_owned_by_websocket_plugin(tmp_path) -> None:
    composition = await build_composition(
        {
            "sessions_dir": str(tmp_path / "sessions"),
            "plugins": [{"id": "websocket-channel", "config": {"port": 48701}}],
        }
    )
    try:
        channel = composition.context.get("channels").manager.get("ws")
        assert channel.port == 48701
    finally:
        await composition.close()


@pytest.mark.asyncio
async def test_agent_service_stays_loadable_without_optional_inbox(tmp_path) -> None:
    composition = await build_composition(
        {
            "sessions_dir": str(tmp_path / "sessions"),
            "plugins": [{"id": "inbox", "disabled": True}],
        }
    )
    try:
        assert composition.context.get("inbox", strict=False) is None
        assert composition.context.get("agents").driver is not None
        assert composition.context.get("agent_runtime", strict=False) is None
        status = {item.id: item for item in composition.plugins.statuses()}
        assert status["agents"].state.name == "ACTIVE"
    finally:
        await composition.close()
