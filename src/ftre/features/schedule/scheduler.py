"""Periodic cron evaluation and delivery for the Schedule Feature."""
# CronScheduler：周期扫描 Schedule 任务，对每个到期任务：
#   1. 先标记"已触发"（append_run，防止重复投递）；
#   2. 创建独立 cron session；
#   3. 构造 BusMessage 投递到消息总线（AgentLoop 消费后在该 session 执行 prompt）。
# 调度循环每 scan_interval 秒 tick 一次，单 tick 内加锁串行执行。

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
        self.scan_interval = max(float(scan_interval), 0.01)  # 防止 0/负值死循环空转
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()  # 串行化显式 tick 与后台 tick

    def start(self) -> None:
        """Start at most one background scan task."""
        # 幂等：已有存活任务时不重复创建
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="ftre-schedule")
        logger.info("[schedule] scheduler started (interval=%ss)", self.scan_interval)

    async def stop(self) -> None:
        """Cancel and await the scan task; repeated calls are harmless."""
        # 幂等停止：取消后台循环并等待其退出
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
        # 单次调度：遍历所有未禁用任务，找到"下次触发时间 ≤ now"的任务投递。
        # 用 _tick_lock 保证并发 tick（后台循环 + 外部显式调用）不重复投递。
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
                # 基准时间：上次运行（run_history 末尾）或创建时间
                base = self._last_run(job)
                try:
                    next_ts = croniter(cron_expr, base).get_next(ret_type=float)
                except Exception as exc:  # noqa: BLE001 - malformed persisted jobs are isolated
                    # 损坏的任务单独跳过，不影响其他任务
                    logger.warning("[schedule] job %s 解析失败: %s", job_id, exc)
                    continue
                # 未到期
                if next_ts > current:
                    continue
                # Mark before delivery so a second scan cannot duplicate a
                # trigger. The lock also serializes explicit concurrent ticks.
                # 先标记再投递：即使投递失败，本周期也不会被重复触发
                self.schedule.append_run(job_id, current)
                # 每个触发创建独立 session（channel=cron），prompt 作为首条 user 消息
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
                # 投递到消息总线：AgentLoop 消费后在该 session 执行 prompt
                await self.message_bus.publish_inbound(message)
                triggered += 1
                logger.info("[schedule] triggered %s -> session=%s", job_id, session_id)
            return triggered

    async def _loop(self) -> None:
        """后台循环：每 scan_interval 秒 tick 一次，单次失败不中断循环。"""
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    # tick 内异常只记录，保证调度循环永远活着
                    logger.exception("[schedule] tick failed")
                await asyncio.sleep(self.scan_interval)
        except asyncio.CancelledError:
            return

    @staticmethod
    def _last_run(job: dict[str, Any]) -> float:
        """取任务基准时间：run_history 最后一条（上次触发）或 created_at。"""
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
