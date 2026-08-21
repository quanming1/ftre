from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ftre.features.schedule.router import build_router
from ftre.features.schedule.service import ScheduleService


def test_router_uses_schedule_service_crud(tmp_path) -> None:
    app = FastAPI()
    app.include_router(build_router(ScheduleService(tmp_path)), prefix="/api")
    with TestClient(app) as client:
        response = client.post(
            "/api/cron",
            json={"cron": "* * * * *", "title": "demo", "prompt": "run"},
        )
        assert response.status_code == 201
        job = response.json()
        assert client.get(f"/api/cron/{job['id']}").json()["title"] == "demo"
        assert client.patch(f"/api/cron/{job['id']}", json={"disabled": True}).json()["disabled"] is True
        assert client.delete(f"/api/cron/{job['id']}").status_code == 204
        assert client.get(f"/api/cron/{job['id']}").status_code == 404
