"""Filesystem persistence owned exclusively by the Schedule Feature.

The store deliberately knows nothing about HTTP, tools, channels, or the
scheduler. Keeping path validation and atomic JSON replacement here gives all
Schedule consumers the same durability and path-traversal boundary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DEFAULT_ROOT = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "cron"


class ScheduleStoreError(RuntimeError):
    """A job document exists but cannot be read or written safely."""


class CronStore:
    """Persist one cron job per JSON document below a fixed root directory."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or DEFAULT_ROOT).expanduser().resolve()

    def list(self) -> list[dict[str, Any]]:
        """Return valid job documents, ignoring malformed recovery debris."""
        self.root.mkdir(parents=True, exist_ok=True)
        jobs: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                logger.warning("[schedule-store] 跳过非普通文件: %s", path.name)
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("[schedule-store] 读取失败 %s: %s", path.name, exc)
                continue
            if isinstance(value, dict):
                jobs.append(value)
            else:
                logger.warning("[schedule-store] 忽略非对象任务: %s", path.name)
        def created_at(item: dict[str, Any]) -> float:
            try:
                return float(item.get("created_at", 0))
            except (TypeError, ValueError):
                return 0.0

        return sorted(jobs, key=created_at, reverse=True)

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Read one job or return ``None`` when it does not exist."""
        path = self._job_path(job_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ScheduleStoreError(f"任务文件不是普通文件: {job_id}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScheduleStoreError(f"读取任务失败: {job_id}") from exc
        if not isinstance(value, dict):
            raise ScheduleStoreError(f"任务文件必须是 JSON 对象: {job_id}")
        return value

    def save(self, job: dict[str, Any]) -> None:
        """Atomically replace one job document after validating its ID."""
        if not isinstance(job, dict):
            raise TypeError("job 必须是对象")
        path = self._job_path(job.get("id"))
        self.root.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix=f".{path.stem}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                json.dump(job, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError as exc:
            raise ScheduleStoreError(f"写入任务失败: {job.get('id')}") from exc
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    logger.warning("[schedule-store] 临时文件清理失败: %s", temporary)

    def delete(self, job_id: str) -> bool:
        """Delete a job document and report whether it existed."""
        path = self._job_path(job_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ScheduleStoreError(f"删除任务失败: {job_id}") from exc
        return True

    def append_run(self, job_id: str, timestamp: float | None = None) -> None:
        """Append a run timestamp through the same atomic write boundary."""
        job = self.get(job_id)
        if job is None:
            return
        history = job.get("run_history")
        if not isinstance(history, list):
            history = []
        history.append(float(timestamp if timestamp is not None else time.time()))
        job["run_history"] = history
        self.save(job)

    def _job_path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            raise ValueError("非法 job_id：只能包含字母、数字、下划线和短横线")
        path = self.root / f"{job_id}.json"
        # The regex is the primary boundary; this assertion documents the
        # invariant for future changes to the ID policy.
        if path.parent != self.root:
            raise ValueError("job_id 超出 Schedule Store 根目录")
        return path
