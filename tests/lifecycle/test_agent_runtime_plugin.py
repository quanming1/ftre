"""F13 Agent runtime Provider Plugin lifecycle coverage."""

import pytest

from ftre.app.gateway.composition import build_composition


@pytest.mark.asyncio
async def test_agent_runtime_is_composed_and_detached_by_plugin(tmp_path) -> None:
    composition = await build_composition({"sessions_dir": str(tmp_path / "sessions")})
    runtime = composition.context.get("agent_runtime")
    agents = composition.context.get("agents")
    assert runtime is not None
    assert runtime.loop is not None
    assert agents.driver is runtime.driver
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
async def test_agent_runtime_stays_loadable_without_optional_inbox(tmp_path) -> None:
    composition = await build_composition(
        {
            "sessions_dir": str(tmp_path / "sessions"),
            "plugins": [{"id": "inbox", "disabled": True}],
        }
    )
    try:
        assert composition.context.get("inbox", strict=False) is None
        runtime = composition.context.get("agent_runtime")
        assert runtime is not None
        assert composition.context.get("agents").driver is runtime.driver
        status = {item.id: item for item in composition.plugins.statuses()}
        assert status["agent-runtime"].state.name == "ACTIVE"
    finally:
        await composition.close()
