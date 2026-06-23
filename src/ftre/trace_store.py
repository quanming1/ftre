"""Read-only query helpers for the append-only agent trace JSONL file."""
from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from ftre.config import CONFIG_PATH

logger = logging.getLogger(__name__)

TRACE_PATH = CONFIG_PATH.parent / "traces" / "agent-traces.jsonl"
RECENT_SCAN_BYTES = 64 * 1024 * 1024
RECENT_SCAN_RECORDS = 50000
_cache_lock = threading.Lock()
_cache: dict[Path, tuple[int, int, list[dict]]] = {}


def load_completed_runs(path: Path = TRACE_PATH) -> list[dict]:
    """Load the latest completed snapshot for every run.

    Corrupt or partially-written lines are ignored so a tracing failure can
    never make the diagnostics API unavailable.
    """
    if not path.exists():
        return []

    try:
        stat = path.stat()
    except OSError:
        return []
    with _cache_lock:
        cached = _cache.get(path)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]

    runs: dict[str, dict] = {}
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[trace-store] skip invalid JSONL line %s", line_number)
                    continue
                if record.get("phase") != "end":
                    continue
                run = record.get("run")
                if isinstance(run, dict) and run.get("id"):
                    runs[str(run["id"])] = run
    except OSError:
        logger.exception("[trace-store] failed to read %s", path)
        return []
    result = list(runs.values())
    with _cache_lock:
        _cache[path] = (stat.st_mtime_ns, stat.st_size, result)
    return result


def list_trace_summaries(path: Path = TRACE_PATH, *, limit: int = 100) -> list[dict]:
    runs = load_recent_completed_runs(path, target_traces=limit)
    return _summarize_runs(runs, limit=limit)


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
        response_models = sorted({
            str(((run.get("outputs") or {}).get("response_metadata") or {}).get("model"))
            for run in llm_runs
            if ((run.get("outputs") or {}).get("response_metadata") or {}).get("model")
        })
        summaries.append({
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
            "error_count": sum(1 for run in trace_runs if run.get("status") == "error"),
        })

    summaries.sort(key=lambda item: item.get("start_time") or "", reverse=True)
    return summaries[: max(1, min(limit, 500))]


def load_recent_completed_runs(
    path: Path = TRACE_PATH,
    *,
    target_traces: int = 100,
    max_bytes: int = RECENT_SCAN_BYTES,
    max_records: int = RECENT_SCAN_RECORDS,
) -> list[dict]:
    """Load completed runs for the newest traces by scanning JSONL from the end.

    The trace file can grow to gigabytes. The Agent Traces list only needs
    recent traces, and root agent runs are appended after their child LLM/tool
    runs. Scanning backward lets the UI avoid a full-file read on every refresh.
    """
    target = max(1, min(target_traces, 500))
    runs_by_id: dict[str, dict] = {}
    root_trace_order: list[str] = []
    root_trace_ids: set[str] = set()

    for record in _iter_recent_jsonl_records(path, max_bytes=max_bytes, max_records=max_records):
        if record.get("phase") != "end":
            continue
        run = record.get("run")
        if not isinstance(run, dict) or not run.get("id"):
            continue
        run_id = str(run["id"])
        runs_by_id.setdefault(run_id, run)
        trace_id = str(run.get("trace_id") or "")
        if (
            trace_id
            and run.get("parent_run_id") is None
            and trace_id not in root_trace_ids
        ):
            root_trace_ids.add(trace_id)
            root_trace_order.append(trace_id)
            if len(root_trace_order) > target:
                break

    selected = set(root_trace_order[:target])
    if not selected:
        selected = {
            str(run.get("trace_id"))
            for run in runs_by_id.values()
            if run.get("trace_id")
        }
    return [
        run
        for run in runs_by_id.values()
        if str(run.get("trace_id") or "") in selected
    ]


def _compact_run(run: dict) -> dict:
    """Strip potentially large payloads while keeping tree diagnostics useful."""
    outputs = run.get("outputs") or {}
    compact_outputs = {
        key: outputs[key]
        for key in (
            "success", "done_reason", "iterations", "finish_reason",
            "has_tool_calls", "usage", "response_metadata", "status", "error",
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


def get_trace(trace_id: str, path: Path = TRACE_PATH, *, include_payload: bool = False) -> dict | None:
    runs = _load_recent_trace_runs(trace_id, path)
    if not runs:
        return None
    runs.sort(key=lambda run: (
        run.get("start_time") or "",
        0 if run.get("parent_run_id") is None else 1,
    ))
    if not include_payload:
        runs = [_compact_run(run) for run in runs]
    return {"trace_id": trace_id, "runs": runs}


def get_trace_run(trace_id: str, run_id: str, path: Path = TRACE_PATH) -> dict | None:
    for record in _iter_recent_jsonl_records(path):
        if record.get("phase") != "end":
            continue
        run = record.get("run")
        if (
            isinstance(run, dict)
            and run.get("trace_id") == trace_id
            and run.get("id") == run_id
        ):
            return run
    return None


def _load_recent_trace_runs(trace_id: str, path: Path = TRACE_PATH) -> list[dict]:
    runs_by_id: dict[str, dict] = {}
    seen_target_root = False
    seen_older_root_after_target = False

    for record in _iter_recent_jsonl_records(path):
        if record.get("phase") != "end":
            continue
        run = record.get("run")
        if not isinstance(run, dict) or not run.get("id"):
            continue
        is_root = run.get("parent_run_id") is None
        current_trace_id = str(run.get("trace_id") or "")
        if current_trace_id == trace_id:
            runs_by_id.setdefault(str(run["id"]), run)
            if is_root:
                seen_target_root = True
        elif seen_target_root and is_root:
            seen_older_root_after_target = True
        if seen_target_root and seen_older_root_after_target:
            break

    return list(runs_by_id.values())


def _iter_recent_jsonl_records(
    path: Path,
    *,
    max_bytes: int = RECENT_SCAN_BYTES,
    max_records: int = RECENT_SCAN_RECORDS,
) -> Iterable[dict]:
    if not path.exists():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= 0:
        return

    chunk_size = 1024 * 1024
    position = size
    pending = b""
    scanned = 0
    yielded = 0
    first_chunk = True

    try:
        with path.open("rb") as stream:
            while position > 0 and scanned < max_bytes and yielded < max_records:
                read_size = min(chunk_size, position, max_bytes - scanned)
                position -= read_size
                stream.seek(position)
                data = stream.read(read_size) + pending
                scanned += read_size
                parts = data.split(b"\n")
                pending = parts[0]
                lines = parts[1:]
                if first_chunk and lines and lines[-1] == b"":
                    lines = lines[:-1]
                first_chunk = False
                for raw in reversed(lines):
                    if yielded >= max_records:
                        return
                    record = _decode_jsonl_line(raw)
                    if record is None:
                        continue
                    yielded += 1
                    yield record

            if position == 0 and pending and yielded < max_records:
                record = _decode_jsonl_line(pending)
                if record is not None:
                    yield record
    except OSError:
        logger.exception("[trace-store] failed to read tail of %s", path)


def _decode_jsonl_line(raw: bytes) -> dict | None:
    if not raw.strip():
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None
