"""SQLite-backed storage and query helpers for Agent Traces."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ftre.config import CONFIG_PATH

if TYPE_CHECKING:
    from ftre_agent_core.tracing import TraceRun

logger = logging.getLogger(__name__)

TRACE_DB_PATH = CONFIG_PATH.parent / "traces" / "agent-traces.sqlite"
_SCHEMA_VERSION = 1


class SQLiteTraceExporter:
    """Trace exporter that stores run snapshots in a standalone SQLite DB."""

    def __init__(self, path: str | Path = TRACE_DB_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._initialized = False

    def on_run_start(self, run: "TraceRun") -> None:
        self._write(run)

    def on_run_end(self, run: "TraceRun") -> None:
        self._write(run)

    def _write(self, run: "TraceRun") -> None:
        try:
            with self._lock:
                conn = _connect(self.path)
                try:
                    _ensure_schema(conn)
                    _upsert_run(conn, run.to_dict())
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            logger.exception(
                "[trace-store] failed to write trace run %s", getattr(run, "id", "")
            )


def list_trace_summaries(
    path: Path = TRACE_DB_PATH,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Return one page of recent trace summaries."""
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM trace_runs WHERE parent_run_id IS NULL"
            ).fetchone()[0]
        )
        roots = conn.execute(
            """
            SELECT trace_id
            FROM trace_runs
            WHERE parent_run_id IS NULL
            ORDER BY start_time DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        trace_ids = [str(row["trace_id"]) for row in roots]
        runs = _load_runs_for_traces(conn, trace_ids)
        traces = _summarize_runs(runs, limit=limit)
        next_offset = offset + len(trace_ids)
        return {
            "traces": traces,
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": next_offset if next_offset < total else None,
            "has_more": next_offset < total,
        }
    finally:
        conn.close()


def get_trace(
    trace_id: str, path: Path = TRACE_DB_PATH, *, include_payload: bool = False
) -> dict | None:
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        runs = _load_runs_for_traces(conn, [trace_id])
    finally:
        conn.close()
    if not runs:
        return None
    runs.sort(
        key=lambda run: (
            run.get("start_time") or "",
            0 if run.get("parent_run_id") is None else 1,
        )
    )
    if not include_payload:
        runs = [_compact_run(run) for run in runs]
    return {"trace_id": trace_id, "runs": runs}


def get_trace_run(
    trace_id: str, run_id: str, path: Path = TRACE_DB_PATH
) -> dict | None:
    conn = _connect(path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM trace_runs WHERE trace_id = ? AND id = ?",
            (trace_id, run_id),
        ).fetchone()
        return _row_to_run(row) if row else None
    finally:
        conn.close()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


_SCHEMA_VERSION_KEY = "schema_version"
_SCHEMA_VERSION_VALUE = str(_SCHEMA_VERSION)

_schema_ready = False
_schema_lock = threading.Lock()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if missing; update schema_version only when needed.

    Uses a module-level flag so that after the first successful call every
    subsequent call (from both reader and writer paths) is a cheap no-op,
    eliminating DDL write-lock contention with the trace exporter.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trace_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trace_runs (
                id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                parent_run_id TEXT,
                name TEXT NOT NULL,
                run_type TEXT NOT NULL,
                status TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                duration_ms REAL,
                error TEXT,
                inputs_json TEXT NOT NULL DEFAULT '{}',
                outputs_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                tags_json TEXT NOT NULL DEFAULT '[]',
                events_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE INDEX IF NOT EXISTS idx_trace_runs_root_recent
                ON trace_runs(parent_run_id, start_time DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_trace_runs_trace
                ON trace_runs(trace_id, start_time, id);
            CREATE INDEX IF NOT EXISTS idx_trace_runs_trace_run
                ON trace_runs(trace_id, id);
            """
        )
        # Only write when the version is missing or mismatched — avoid pointless
        # write contention with the trace exporter thread.
        row = conn.execute(
            "SELECT value FROM trace_meta WHERE key = ?",
            (_SCHEMA_VERSION_KEY,),
        ).fetchone()
        if row is None or row["value"] != _SCHEMA_VERSION_VALUE:
            conn.execute(
                "INSERT OR REPLACE INTO trace_meta(key, value) VALUES(?, ?)",
                (_SCHEMA_VERSION_KEY, _SCHEMA_VERSION_VALUE),
            )
        _schema_ready = True


def _upsert_run(conn: sqlite3.Connection, run: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO trace_runs (
            id, trace_id, parent_run_id, name, run_type, status,
            start_time, end_time, duration_ms, error,
            inputs_json, outputs_json, metadata_json, tags_json, events_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            trace_id = excluded.trace_id,
            parent_run_id = excluded.parent_run_id,
            name = excluded.name,
            run_type = excluded.run_type,
            status = excluded.status,
            start_time = excluded.start_time,
            end_time = excluded.end_time,
            duration_ms = excluded.duration_ms,
            error = excluded.error,
            inputs_json = excluded.inputs_json,
            outputs_json = excluded.outputs_json,
            metadata_json = excluded.metadata_json,
            tags_json = excluded.tags_json,
            events_json = excluded.events_json
        """,
        (
            run["id"],
            run["trace_id"],
            run.get("parent_run_id"),
            run.get("name") or "react_agent",
            run.get("run_type") or "agent",
            run.get("status") or "running",
            run.get("start_time") or "",
            run.get("end_time"),
            run.get("duration_ms"),
            run.get("error"),
            _json_dump(run.get("inputs") or {}),
            _json_dump(run.get("outputs") or {}),
            _json_dump(run.get("metadata") or {}),
            _json_dump(run.get("tags") or []),
            _json_dump(run.get("events") or []),
        ),
    )


def _load_runs_for_traces(conn: sqlite3.Connection, trace_ids: list[str]) -> list[dict]:
    if not trace_ids:
        return []
    placeholders = ",".join("?" for _ in trace_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM trace_runs
        WHERE trace_id IN ({placeholders})
        ORDER BY trace_id, start_time, parent_run_id IS NOT NULL, id
        """,
        trace_ids,
    ).fetchall()
    return [_row_to_run(row) for row in rows]


def _summarize_runs(runs: Iterable[dict], *, limit: int) -> list[dict]:
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        trace_id = str(run.get("trace_id") or "")
        if trace_id:
            by_trace[trace_id].append(run)

    summaries: list[dict] = []
    for trace_id, trace_runs in by_trace.items():
        root = next(
            (run for run in trace_runs if run.get("parent_run_id") is None),
            min(trace_runs, key=lambda run: run.get("start_time") or ""),
        )
        llm_runs = [run for run in trace_runs if run.get("run_type") == "llm"]
        tool_runs = [run for run in trace_runs if run.get("run_type") == "tool"]
        stop_without_tools = sum(
            1
            for run in llm_runs
            if (run.get("outputs") or {}).get("finish_reason") == "stop"
            and not (run.get("outputs") or {}).get("has_tool_calls")
        )
        response_models = sorted(
            {
                str(
                    ((run.get("outputs") or {}).get("response_metadata") or {}).get(
                        "model"
                    )
                )
                for run in llm_runs
                if ((run.get("outputs") or {}).get("response_metadata") or {}).get(
                    "model"
                )
            }
        )
        summaries.append(
            {
                "trace_id": trace_id,
                "name": root.get("name") or "react_agent",
                "status": root.get("status") or "unknown",
                "start_time": root.get("start_time"),
                "end_time": root.get("end_time"),
                "duration_ms": root.get("duration_ms"),
                "metadata": root.get("metadata") or {},
                "tags": root.get("tags") or [],
                "outputs": root.get("outputs") or {},
                "run_count": len(trace_runs),
                "llm_run_count": len(llm_runs),
                "tool_run_count": len(tool_runs),
                "stop_without_tools": stop_without_tools,
                "response_models": response_models,
                "error_count": sum(
                    1 for run in trace_runs if run.get("status") == "error"
                ),
            }
        )

    summaries.sort(key=lambda item: item.get("start_time") or "", reverse=True)
    return summaries[: max(1, min(limit, 500))]


def _compact_run(run: dict) -> dict:
    """Strip potentially large payloads while keeping tree diagnostics useful."""
    outputs = run.get("outputs") or {}
    compact_outputs = {
        key: outputs[key]
        for key in (
            "success",
            "done_reason",
            "iterations",
            "finish_reason",
            "has_tool_calls",
            "usage",
            "response_metadata",
            "status",
            "error",
        )
        if key in outputs
    }
    tool_calls = outputs.get("tool_calls")
    if isinstance(tool_calls, list):
        compact_outputs["tool_call_count"] = len(tool_calls)
    return {
        **run,
        "inputs": {},
        "outputs": compact_outputs,
        "events": [],
        "payload_loaded": False,
    }


def _row_to_run(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "trace_id": row["trace_id"],
        "parent_run_id": row["parent_run_id"],
        "name": row["name"],
        "run_type": row["run_type"],
        "status": row["status"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "duration_ms": row["duration_ms"],
        "inputs": _json_load(row["inputs_json"], {}),
        "outputs": _json_load(row["outputs_json"], {}),
        "error": row["error"],
        "metadata": _json_load(row["metadata_json"], {}),
        "tags": _json_load(row["tags_json"], []),
        "events": _json_load(row["events_json"], []),
    }


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default
