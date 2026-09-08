from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from ftre.services.session.router import build_router
from ftre.services.session.service import ForkResult, RollbackResult


def _app(sessions, agents=None):
    app = FastAPI()
    app.include_router(build_router(sessions, agents or SimpleNamespace(), None), prefix="/api")
    return app


@pytest.mark.asyncio
async def test_fork_route_passes_message_boundary_and_returns_snapshot_coordinates():
    sessions = SimpleNamespace(
        get_session=AsyncMock(return_value={"metadata": {}}),
        fork_session=AsyncMock(
            return_value=ForkResult(
                fork_session_id="ws_sess_child",
                title="fork of parent",
                workspace="E:/repo",
                parent_session_id="ws_sess_parent",
                through_message_id="assistant-1",
                seq=12,
            )
        ),
    )
    agents = SimpleNamespace(is_session_busy=lambda _sid: False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, agents)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/sessions/ws_sess_parent/fork",
            json={"through_message_id": "assistant-1"},
        )

    assert response.status_code == 200
    assert response.json()["fork_session_id"] == "ws_sess_child"
    assert response.json()["seq"] == 12
    sessions.fork_session.assert_awaited_once_with(
        "ws_sess_parent",
        through_message_id="assistant-1",
    )


@pytest.mark.asyncio
async def test_fork_route_rejects_busy_session_before_copying():
    sessions = SimpleNamespace(
        get_session=AsyncMock(return_value={"metadata": {}}),
        fork_session=AsyncMock(),
    )
    agents = SimpleNamespace(is_session_busy=lambda _sid: True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, agents)),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/sessions/ws_sess_parent/fork")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_busy"
    sessions.fork_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_route_rewrites_current_session_without_forking():
    sessions = SimpleNamespace(
        get_session=AsyncMock(return_value={"metadata": {}}),
        rollback_session=AsyncMock(
            return_value=RollbackResult(
                session_id="ws_sess_parent",
                through_message_id="user-1",
                seq=12,
                removed_message_ids=["user-1", "assistant-1"],
                prefill_content=[{"type": "text", "text": "retry"}],
                title="parent",
                workspace="E:/repo",
            )
        ),
    )
    agents = SimpleNamespace(is_session_busy=lambda _sid: False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(sessions, agents)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/sessions/ws_sess_parent/rollback",
            json={"through_message_id": "user-1"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "ws_sess_parent"
    assert body["prefill_content"][0]["text"] == "retry"
    sessions.rollback_session.assert_awaited_once_with(
        "ws_sess_parent",
        through_message_id="user-1",
    )
