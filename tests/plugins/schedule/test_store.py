from __future__ import annotations

import json

import pytest

from ftre.plugins.builtin.schedule.store import CronStore, ScheduleStoreError


def _job(job_id: str = "job_1") -> dict:
    return {
        "id": job_id,
        "cron": "* * * * *",
        "title": "demo",
        "prompt": "run once",
        "disabled": False,
        "created_at": 0.0,
        "run_history": [],
    }


def test_store_crud_and_atomic_run_history(tmp_path) -> None:
    store = CronStore(tmp_path)
    store.save(_job())
    assert store.get("job_1")["title"] == "demo"
    store.append_run("job_1", 123.5)
    assert store.get("job_1")["run_history"] == [123.5]
    assert [item["id"] for item in store.list()] == ["job_1"]
    assert store.delete("job_1") is True
    assert store.delete("job_1") is False
    assert list(tmp_path.glob("*.tmp")) == []


def test_store_rejects_path_traversal_and_ignores_corrupt_list_entry(tmp_path) -> None:
    store = CronStore(tmp_path)
    with pytest.raises(ValueError):
        store.get("../outside")
    (tmp_path / "broken.json").write_text("{broken", encoding="utf-8")
    assert store.list() == []
    with pytest.raises(ScheduleStoreError):
        store.get("broken")


def test_store_writes_compatible_json_shape(tmp_path) -> None:
    store = CronStore(tmp_path)
    store.save(_job("job_compatible"))
    payload = json.loads((tmp_path / "job_compatible.json").read_text(encoding="utf-8"))
    assert set(payload) == {
        "id", "cron", "title", "prompt", "disabled", "created_at", "run_history"
    }
