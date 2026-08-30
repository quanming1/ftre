from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ftre.services.config.router import build_router
from ftre.services.config.service import ConfigService


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_external_config_edit_enters_snapshot_and_model_catalog(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write(
        path,
        {
            "providers": {
                "demo": {
                    "api_key": "secret-value",
                    "models": [{"id": "old", "name": "Old"}],
                }
            }
        },
    )
    service = ConfigService(path)
    assert service.snapshot().revision == 0

    _write(
        path,
        {
            "providers": {
                "demo": {
                    "api_key": "secret-value",
                    "models": [
                        {"id": "old", "name": "Old"},
                        {"id": "new", "name": "New", "context_window": 200000},
                    ],
                }
            }
        },
    )

    snapshot = service.snapshot()
    assert snapshot.revision == 1
    assert snapshot.source_path == str(path.resolve())
    assert len(snapshot.content_hash) == 64
    catalog = service.model_catalog()
    assert catalog["revision"] == 1
    assert catalog["providers"][0]["models"][-1]["id"] == "new"
    assert "api_key" not in json.dumps(catalog)


def test_invalid_external_config_keeps_last_valid_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write(path, {"providers": {"demo": {"models": [{"id": "old"}]}}})
    service = ConfigService(path)
    before = service.snapshot()

    path.write_text("{ invalid", encoding="utf-8")
    invalid = service.snapshot()
    assert invalid.revision == before.revision
    assert invalid.value == before.value

    _write(path, {"providers": {"demo": {"models": [{"id": "fixed"}]}}})
    fixed = service.snapshot()
    assert fixed.revision == before.revision + 1
    assert fixed.value["providers"]["demo"]["models"][0]["id"] == "fixed"


@pytest.mark.asyncio
async def test_external_watcher_notifies_once_and_closes_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write(path, {"providers": {"demo": {"models": [{"id": "old"}]}}})
    service = ConfigService(path)
    revisions: list[int] = []
    service.watch(lambda snapshot: revisions.append(snapshot.revision))
    service.start_watcher(0.02)

    _write(path, {"providers": {"demo": {"models": [{"id": "new"}]}}})
    await asyncio.sleep(0.08)
    await service.reload()
    assert revisions == [1]

    await service.close()
    assert service._watcher_task is None


def test_model_catalog_router_returns_revision_and_no_credentials(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write(
        path,
        {
            "providers": {
                "demo": {
                    "api_key": "secret-value",
                    "api_base": "https://example.invalid",
                    "models": [{"id": "demo-model", "name": "Demo"}],
                }
            }
        },
    )
    app = FastAPI()
    app.include_router(build_router(ConfigService(path)), prefix="/api")
    with TestClient(app) as client:
        response = client.get("/api/config/models")
    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 0
    assert body["providers"][0]["models"][0]["id"] == "demo-model"
    assert "api_key" not in response.text
