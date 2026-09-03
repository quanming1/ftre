"""Lightweight, provider-neutral tracing for agent runs.

The data model follows the same core idea as LangSmith: a trace is a tree of
runs. Exporters decide where completed run snapshots are stored. Tracing is
disabled by default and exporter failures never affect agent execution.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)


class RunType(str, Enum):
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class TraceEvent:
    name: str
    time: datetime = field(default_factory=_utc_now)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "time": self.time.isoformat(),
            "data": _json_safe(self.data),
        }


@dataclass
class TraceRun:
    id: str
    trace_id: str
    name: str
    run_type: RunType
    start_time: datetime
    parent_run_id: str | None = None
    end_time: datetime | None = None
    status: RunStatus = RunStatus.RUNNING
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)

    @property
    def duration_ms(self) -> float | None:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time).total_seconds() * 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_run_id": self.parent_run_id,
            "name": self.name,
            "run_type": self.run_type.value,
            "status": self.status.value,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "inputs": _json_safe(self.inputs),
            "outputs": _json_safe(self.outputs),
            "error": self.error,
            "metadata": _json_safe(self.metadata),
            "tags": list(self.tags),
            "events": [event.to_dict() for event in self.events],
        }


class TraceExporter(Protocol):
    def on_run_start(self, run: TraceRun) -> None: ...

    def on_run_end(self, run: TraceRun) -> None: ...


class TraceSpan:
    def __init__(self, tracer: Tracer, run: TraceRun):
        self._tracer = tracer
        self.run = run

    @property
    def id(self) -> str:
        return self.run.id

    @property
    def trace_id(self) -> str:
        return self.run.trace_id

    @property
    def ended(self) -> bool:
        return self.run.end_time is not None

    def child(
        self,
        name: str,
        run_type: RunType,
        *,
        inputs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> TraceSpan:
        return self._tracer.start_run(
            name,
            run_type,
            parent=self,
            inputs=inputs,
            metadata=metadata,
            tags=tags,
        )

    def add_event(self, name: str, data: dict[str, Any] | None = None) -> None:
        if not self.ended:
            self.run.events.append(TraceEvent(name=name, data=data or {}))

    def end(
        self,
        *,
        outputs: dict[str, Any] | None = None,
        error: BaseException | str | None = None,
        status: RunStatus | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self.ended:
            return
        if outputs:
            self.run.outputs.update(outputs)
        if metadata:
            self.run.metadata.update(metadata)
        if error is not None:
            self.run.error = str(error)
            self.run.status = RunStatus.ERROR
        else:
            self.run.status = status or RunStatus.COMPLETED
        self.run.end_time = _utc_now()
        self._tracer._notify_end(self.run)


class Tracer:
    """Creates trace runs and publishes immutable snapshots to exporters."""

    def __init__(self, exporters: list[TraceExporter] | None = None):
        self.exporters = list(exporters or [])

    @property
    def enabled(self) -> bool:
        return bool(self.exporters)

    def start_run(
        self,
        name: str,
        run_type: RunType,
        *,
        parent: TraceSpan | None = None,
        inputs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> TraceSpan:
        run_id = str(uuid.uuid4())
        run = TraceRun(
            id=run_id,
            trace_id=parent.trace_id if parent else run_id,
            parent_run_id=parent.id if parent else None,
            name=name,
            run_type=run_type,
            start_time=_utc_now(),
            inputs=copy.deepcopy(inputs or {}),
            metadata=copy.deepcopy(metadata or {}),
            tags=list(tags or []),
        )
        self._notify("on_run_start", run)
        return TraceSpan(self, run)

    def _notify_end(self, run: TraceRun) -> None:
        self._notify("on_run_end", run)

    def _notify(self, method: str, run: TraceRun) -> None:
        if not self.exporters:
            return
        snapshot = copy.deepcopy(run)
        for exporter in self.exporters:
            try:
                getattr(exporter, method)(copy.deepcopy(snapshot))
            except Exception:
                logger.exception("trace exporter %s failed during %s", type(exporter).__name__, method)


class InMemoryTraceExporter:
    """Exporter intended for tests, debugging, and embedding applications."""

    def __init__(self):
        self.runs: dict[str, TraceRun] = {}
        self._lock = threading.Lock()

    def on_run_start(self, run: TraceRun) -> None:
        with self._lock:
            self.runs[run.id] = run

    def on_run_end(self, run: TraceRun) -> None:
        with self._lock:
            self.runs[run.id] = run

    def get_trace(self, trace_id: str) -> list[TraceRun]:
        with self._lock:
            runs = [copy.deepcopy(run) for run in self.runs.values() if run.trace_id == trace_id]
        return sorted(runs, key=lambda run: run.start_time)


class JsonlTraceExporter:
    """Append-only JSONL exporter. One record is written for start and end."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def on_run_start(self, run: TraceRun) -> None:
        self._write("start", run)

    def on_run_end(self, run: TraceRun) -> None:
        self._write("end", run)

    def _write(self, phase: str, run: TraceRun) -> None:
        record = {"phase": phase, "run": run.to_dict()}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
