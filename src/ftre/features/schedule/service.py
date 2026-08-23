"""Public Schedule Service coordinating validation and the CronStore."""
# ScheduleService：校验 cron/任务输入并委托 CronStore 持久化；
# 不直接操作 HTTP、Channel 或调度循环——它们是别的模块的职责。
# 对外是唯一的 Job CRUD API，调用方不应直接访问 CronStore.root。

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from croniter import croniter

from .store import CronStore

# 允许被外部修改的字段集合（create/update 都受它约束）
_MUTABLE_FIELDS = frozenset({"cron", "title", "prompt", "disabled"})


class ScheduleService:
    """唯一的 Job CRUD API；调用方不应直接访问 ``CronStore.root``。"""
    key = "schedule"

    def __init__(self, root: str | Path | None = None, *, store: CronStore | None = None) -> None:
        # 可注入 store（测试用）；默认以 root 新建 CronStore
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
        # 只允许受控字段 + 只读字段（id/created_at/run_history 由调用方显式给或自动生成）
        illegal = set(payload) - _MUTABLE_FIELDS - {"id", "created_at", "run_history"}
        if illegal:
            raise ValueError(f"不允许的字段: {sorted(illegal)}")
        values = self._validate_fields(payload, require_all=True)
        # 未提供 id 时自动生成 job_<10位hex>
        job_id = payload.get("id") or f"job_{uuid.uuid4().hex[:10]}"
        if not isinstance(job_id, str):
            raise TypeError("id 必须是字符串")
        # 同 id 不允许重复创建
        if self._store.get(job_id) is not None:
            raise ValueError(f"任务已存在: {job_id}")
        history = payload.get("run_history") or []
        if not isinstance(history, list):
            raise TypeError("run_history 必须是列表")
        job = {
            "id": job_id,
            **values,
            "disabled": bool(values.get("disabled", False)),
            "created_at": float(payload.get("created_at", time.time())),
            "run_history": list(history),
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
        # 校验通过后原地更新并整体原子保存
        job.update(self._validate_fields(patch, require_all=False))
        self._store.save(job)
        return job

    def delete(self, job_id: str) -> bool:
        """Delete one job through the Store."""
        return self._store.delete(job_id)

    def append_run(self, job_id: str, timestamp: float | None = None) -> None:
        """Record a scheduler trigger without exposing persistence details."""
        # 调度器触发后记录时间戳（run_history），供下次调度与诊断使用
        self._store.append_run(job_id, timestamp)

    def close(self) -> None:
        """Release hook for Composition; JSON Store has no open handles."""
        # JSON 存储无打开句柄，close 为空实现（满足 Plugin 生命周期契约）
        return

    @staticmethod
    def _validate_fields(payload: Mapping[str, Any], *, require_all: bool) -> dict[str, Any]:
        """字段级校验：cron/title/prompt 类型与 cron 表达式合法性，disabled 布尔性。"""
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
