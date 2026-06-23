from __future__ import annotations

from ftre.trace_store import SQLiteTraceExporter, get_trace, get_trace_run, list_trace_summaries


class FakeRun:
    def __init__(self, payload: dict):
        self.id = payload["id"]
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)


def _run(
    *,
    trace_id: str,
    run_id: str,
    parent_run_id: str | None,
    run_type: str,
    start_time: str,
    outputs: dict | None = None,
    metadata: dict | None = None,
):
    return {
        "id": run_id,
        "trace_id": trace_id,
        "parent_run_id": parent_run_id,
        "name": "session:sess_1" if parent_run_id is None else run_type,
        "run_type": run_type,
        "status": "completed",
        "start_time": start_time,
        "end_time": start_time,
        "duration_ms": 1000,
        "inputs": {"prompt": "hello"},
        "outputs": outputs or {},
        "error": None,
        "metadata": metadata or {},
        "tags": ["test"],
        "events": [{"name": "checkpoint", "data": {"ok": True}}],
    }


def test_sqlite_trace_store_writes_and_reads_runs(tmp_path):
    db_path = tmp_path / "agent-traces.sqlite"
    exporter = SQLiteTraceExporter(db_path)
    root = _run(
        trace_id="trace-1",
        run_id="root",
        parent_run_id=None,
        run_type="agent",
        start_time="2026-06-22T10:00:00+00:00",
        outputs={"success": True, "done_reason": "completed"},
        metadata={"session_id": "sess_1"},
    )
    llm = _run(
        trace_id="trace-1",
        run_id="llm",
        parent_run_id="root",
        run_type="llm",
        start_time="2026-06-22T10:00:00.5+00:00",
        outputs={
            "finish_reason": "stop",
            "has_tool_calls": False,
            "response_metadata": {"model": "qwen3.7-max"},
        },
    )

    exporter.on_run_end(FakeRun(root))
    exporter.on_run_end(FakeRun(llm))

    page = list_trace_summaries(db_path)
    assert page["total"] == 1
    assert page["has_more"] is False
    assert page["next_offset"] is None
    assert page["traces"][0]["stop_without_tools"] == 1
    assert page["traces"][0]["response_models"] == ["qwen3.7-max"]

    compact = get_trace("trace-1", db_path)
    assert compact["runs"][0]["id"] == "root"
    assert compact["runs"][0]["inputs"] == {}
    assert compact["runs"][1]["outputs"]["finish_reason"] == "stop"

    full_run = get_trace_run("trace-1", "llm", db_path)
    assert full_run["inputs"]["prompt"] == "hello"
    assert full_run["events"][0]["name"] == "checkpoint"
    assert get_trace("missing", db_path) is None


def test_sqlite_trace_store_paginates_root_traces(tmp_path):
    db_path = tmp_path / "agent-traces.sqlite"
    exporter = SQLiteTraceExporter(db_path)
    for idx in range(3):
        trace_id = f"trace-{idx}"
        root_id = f"root-{idx}"
        exporter.on_run_end(FakeRun(_run(
            trace_id=trace_id,
            run_id=f"llm-{idx}",
            parent_run_id=root_id,
            run_type="llm",
            start_time=f"2026-06-22T10:0{idx}:01+00:00",
            outputs={"finish_reason": "stop", "has_tool_calls": False},
        )))
        exporter.on_run_end(FakeRun(_run(
            trace_id=trace_id,
            run_id=root_id,
            parent_run_id=None,
            run_type="agent",
            start_time=f"2026-06-22T10:0{idx}:00+00:00",
            metadata={"session_id": f"sess_{idx}"},
        )))

    first = list_trace_summaries(db_path, limit=2, offset=0)
    assert [item["trace_id"] for item in first["traces"]] == ["trace-2", "trace-1"]
    assert first["total"] == 3
    assert first["has_more"] is True
    assert first["next_offset"] == 2

    second = list_trace_summaries(db_path, limit=2, offset=2)
    assert [item["trace_id"] for item in second["traces"]] == ["trace-0"]
    assert second["has_more"] is False
    assert second["next_offset"] is None
