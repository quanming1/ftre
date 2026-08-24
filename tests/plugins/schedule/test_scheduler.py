from __future__ import annotations

import asyncio

import pytest

from ftre.plugins.builtin.schedule.scheduler import CronScheduler
from ftre.plugins.builtin.schedule.service import ScheduleService
from ftre.services.messaging.bus import MessageBusService


class _Sessions:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def create_session(self, channel_id: str, title: str = "") -> str:
        session_id = f"{channel_id}_{len(self.created) + 1}"
        self.created.append(session_id)
        return session_id


class _Bus:
    def __init__(self) -> None:
        self.messages = []

    async def publish_inbound(self, message) -> None:
        self.messages.append(message)


class _EventBus:
    def __init__(self) -> None:
        self.messages = []

    async def publish_inbound(self, message) -> None:
        self.messages.append(message)


@pytest.mark.asyncio
async def test_scheduler_triggers_once_and_skips_disabled(tmp_path) -> None:
    schedule = ScheduleService(tmp_path)
    schedule.create({
        "id": "job_due",
        "cron": "* * * * *",
        "title": "due",
        "prompt": "do it",
        "created_at": 0,
    })
    schedule.create({
        "id": "job_disabled",
        "cron": "* * * * *",
        "title": "skip",
        "prompt": "no",
        "disabled": True,
        "created_at": 0,
    })
    sessions = _Sessions()
    bus = _Bus()
    scheduler = CronScheduler(schedule, sessions, bus)

    assert await scheduler.tick(now=120) == 1
    assert await scheduler.tick(now=121) == 0
    assert len(sessions.created) == 1
    assert len(bus.messages) == 1
    assert bus.messages[0].data["content"] == "do it"


@pytest.mark.asyncio
async def test_scheduler_concurrent_ticks_do_not_duplicate_or_leave_task(tmp_path) -> None:
    schedule = ScheduleService(tmp_path)
    schedule.create({
        "id": "job_due",
        "cron": "* * * * *",
        "title": "due",
        "prompt": "do it",
        "created_at": 0,
    })
    sessions = _Sessions()
    scheduler = CronScheduler(schedule, sessions, _Bus(), scan_interval=60)
    assert await asyncio.gather(scheduler.tick(now=120), scheduler.tick(now=120)) == [1, 0]
    scheduler.start()
    scheduler.start()
    task = scheduler._task
    assert task is not None and not task.done()
    await scheduler.stop()
    await scheduler.stop()
    assert scheduler._task is None


@pytest.mark.asyncio
async def test_scheduler_uses_message_bus_service(tmp_path) -> None:
    """The real Gateway injects MessageBusService, not the raw EventBus."""
    schedule = ScheduleService(tmp_path)
    schedule.create({
        "id": "job_due",
        "cron": "* * * * *",
        "title": "due",
        "prompt": "do it",
        "created_at": 0,
    })
    underlying = _EventBus()
    sessions = _Sessions()
    scheduler = CronScheduler(schedule, sessions, MessageBusService(underlying))

    assert await scheduler.tick(now=120) == 1
    assert len(underlying.messages) == 1
