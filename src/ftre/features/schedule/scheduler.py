"""Periodic cron evaluation and delivery for the Schedule Feature."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from croniter import croniter

from ftre.services.messaging.bus import BusMessage, MessageBusService
from ftre.services.session.service import SessionService

from .service import ScheduleService

logger = logging.getLogger(__name__)


class CronScheduler:
    """Scan Schedule jobs and publish one inbound message per due job."""

    def __init__(
        self,
        schedule: ScheduleService,
        sessions: SessionService,
        message_bus: MessageBusService,
        *,
        default_channel: str = "cron",
        scan_interval: float = 30,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.schedule = schedule
        self.sessions = sessions
        self.message_bus = message_bus
        self.default_channel = default_channel
        self.scan_interval = max(float(scan_interval), 0.01)
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()

    def start(self) -> None:
        """Start at most one background scan task."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="ftre-schedule")
        logger.info("[schedule] scheduler started (interval=%ss)", self.scan_interval)

    async def stop(self) -> None:
        """Cancel and await the scan task; repeated calls are harmless."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("[schedule] scheduler stopped")

    async def tick(self, now: float | None = None) -> int:
        """Trigger due jobs once and return the number of deliveries."""
        async with self._tick_lock:
            current = float(self.clock() if now is None else now)
            triggered = 0
            for job in self.schedule.list():
                if job.get("disabled"):
                    continue
                cron_expr = job.get("cron", "")
                job_id = job.get("id")
                if not isinstance(job_id, str) or not cron_expr or not croniter.is_valid(cron_expr):
                    continue
                base = self._last_run(job)
                try:
                    next_ts = croniter(cron_expr, base).get_next(ret_type=float)
                except Exception as exc:  # noqa: BLE001 - malformed persisted jobs are isolated
                    logger.warning("[schedule] job %s 解析失败: %s", job_id, exc)
                    continue
                if next_ts > current:
                    continue
                # Mark before delivery so a second scan cannot duplicate a
                # trigger. The lock also serializes explicit concurrent ticks.
                self.schedule.append_run(job_id, current)
                session_id = await self.sessions.create_session(
                    channel_id=self.default_channel,
                    title=f"[cron] {job.get('title', job_id)}",
                )
                message = BusMessage(
                    type="user_message",
                    from_channel=self.default_channel,
                    to_channel=self.default_channel,
                    from_session=session_id,
                    to_session=session_id,
                    data={"content": job.get("prompt", ""), "session_id": session_id},
                )
                await self.message_bus.publish_inbound(message)
                triggered += 1
                logger.info("[schedule] triggered %s -> session=%s", job_id, session_id)
            return triggered

    async def _loop(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("[schedule] tick failed")
                await asyncio.sleep(self.scan_interval)
        except asyncio.CancelledError:
            return

    @staticmethod
    def _last_run(job: dict[str, Any]) -> float:
        history = job.get("run_history")
        if isinstance(history, list) and history:
            try:
                return float(history[-1])
            except (TypeError, ValueError):
                pass
        try:
            return float(job.get("created_at", 0.0))
        except (TypeError, ValueError):
            return 0.0
