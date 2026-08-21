from __future__ import annotations

from fastapi.testclient import TestClient

from ftre.app.gateway.http import create_app
from ftre.services.http import HttpService


def _client() -> TestClient:
    http = HttpService()
    http.register_health()
    return TestClient(create_app(http))


def test_default_cors_allows_desktop_dev_port() -> None:
    with _client() as client:
        response = client.get(
            "/api/health",
            headers={"Origin": "http://localhost:48651"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["access-control-allow-origin"] == "http://localhost:48651"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_default_cors_rejects_non_loopback_origin() -> None:
    with _client() as client:
        response = client.get(
            "/api/health",
            headers={"Origin": "https://example.com"},
        )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_custom_cors_origins_remain_exact() -> None:
    http = HttpService()
    http.register_health()
    with TestClient(create_app(http, cors_origins=["https://desktop.example"])) as client:
        allowed = client.get(
            "/api/health",
            headers={"Origin": "https://desktop.example:48651"},
        )
        exact = client.get(
            "/api/health",
            headers={"Origin": "https://desktop.example"},
        )

    assert "access-control-allow-origin" not in allowed.headers
    assert exact.headers["access-control-allow-origin"] == "https://desktop.example"
