"""Public Schedule Service coordinating validation and the CronStore."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from croniter import croniter

from .store import CronStore

_MUTABLE_FIELDS = frozenset({"cron", "title", "prompt", "disabled"})


class ScheduleService:
    """唯一的 Job CRUD API；调用方不应直接访问 ``CronStore.root``。"""

    key = "schedule"

    def __init__(self, root: str | Path | None = None, *, store: CronStore | None = None) -> None:
        self._store = store or CronStore(root)

    def list(self) -> list[dict[str, Any]]:
        """List persisted jobs in stable newest-first order."""
        return self._store.list()

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Return one job, applying the Store's ID and corruption boundary."""
        return self._store.get(job_id)

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and persist a new job, assigning an ID when absent."""
        if not isinstance(payload, Mapping):
            raise TypeError("任务必须是对象")
        illegal = set(payload) - _MUTABLE_FIELDS - {"id", "created_at", "run_history"}
        if illegal:
            raise ValueError(f"不允许的字段: {sorted(illegal)}")
        values = self._validate_fields(payload, require_all=True)
        job = {
            "id": payload.get("id") or f"job_{uuid.uuid4().hex[:10]}",
            **values,
            "disabled": bool(values.get("disabled", False)),
            "created_at": float(payload.get("created_at", time.time())),
            "run_history": list(payload.get("run_history") or []),
        }
        self._store.save(job)
        return job

    def update(self, job_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and atomically update mutable fields of an existing job."""
        if not isinstance(patch, Mapping):
            raise TypeError("更新内容必须是对象")
        illegal = set(patch) - _MUTABLE_FIELDS
        if illegal:
            raise ValueError(f"不允许修改字段: {sorted(illegal)}")
        if not patch:
            raise ValueError("至少需要更新一个字段")
        job = self._store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        job.update(self._validate_fields(patch, require_all=False))
        self._store.save(job)
        return job

    def delete(self, job_id: str) -> bool:
        """Delete one job through the Store."""
        return self._store.delete(job_id)

    def append_run(self, job_id: str, timestamp: float | None = None) -> None:
        """Record a scheduler trigger without exposing persistence details."""
        self._store.append_run(job_id, timestamp)

    def close(self) -> None:
        """Release hook for Composition; JSON Store has no open handles."""
        return

    @staticmethod
    def _validate_fields(payload: Mapping[str, Any], *, require_all: bool) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for field in ("cron", "title", "prompt"):
            value = payload.get(field)
            if value is None:
                if require_all:
                    raise ValueError(f"缺少字段: {field}")
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} 不能为空")
            value = value.strip()
            if field == "cron" and not croniter.is_valid(value):
                raise ValueError(f"无效的 cron 表达式: {value}")
            cleaned[field] = value
        if "disabled" in payload:
            if not isinstance(payload["disabled"], bool):
                raise ValueError("disabled 必须是布尔值")
            cleaned["disabled"] = payload["disabled"]
        return cleaned
