from __future__ import annotations

import pytest

from ftre.app.gateway.composition import build_composition


@pytest.mark.asyncio
async def test_session_route_plugin_unload_and_restart_are_reversible(tmp_path) -> None:
    composition = await build_composition({"sessions_dir": str(tmp_path / "sessions")})
    try:
        http = composition.context.http
        assert any(route["owner"] == "sessions" for route in http.snapshot())
        initial_count = sum(route["owner"] == "sessions" for route in http.snapshot())
        assert await composition.plugins.restart("session-routes") is True
        assert sum(route["owner"] == "sessions" for route in http.snapshot()) == initial_count

        assert await composition.plugins.unload("session-routes") is True
        assert not [route for route in http.snapshot() if route["owner"] == "sessions"]
        assert composition.context.get("sessions") is not None
        assert composition.context.get("agents") is not None
        assert composition.context.get("inbox") is not None
    finally:
        await composition.close()
