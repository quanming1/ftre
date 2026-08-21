from __future__ import annotations

import pytest

from ftre.features.schedule.service import ScheduleService


def test_schedule_service_crud_is_the_public_job_boundary(tmp_path) -> None:
    service = ScheduleService(tmp_path)
    job = service.create({
        "cron": "*/5 * * * *",
        "title": "提醒",
        "prompt": "喝水",
    })
    assert job["id"].startswith("job_")
    assert service.get(job["id"])["disabled"] is False
    updated = service.update(job["id"], {"disabled": True, "title": "新提醒"})
    assert updated["disabled"] is True
    assert updated["title"] == "新提醒"
    assert service.delete(job["id"]) is True
    assert service.get(job["id"]) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "missing cron", "prompt": "x"},
        {"cron": "not cron", "title": "x", "prompt": "y"},
        {"cron": "* * * * *", "title": "x", "prompt": "y", "disabled": "yes"},
    ],
)
def test_schedule_service_validates_job_fields(tmp_path, payload) -> None:
    with pytest.raises((ValueError, TypeError)):
        ScheduleService(tmp_path).create(payload)
