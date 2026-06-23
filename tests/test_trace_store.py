import json

from ftre.trace_store import (
    get_trace,
    get_trace_run,
    list_trace_summaries,
    load_completed_runs,
    load_recent_completed_runs,
)


def _write(path, records):
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )


def test_trace_store_groups_completed_runs_and_skips_invalid_lines(tmp_path):
    path = tmp_path / "traces.jsonl"
    records = [
        {"phase": "start", "run": {"id": "root", "trace_id": "trace-1"}},
        {
            "phase": "end",
            "run": {
                "id": "root", "trace_id": "trace-1", "parent_run_id": None,
                "name": "session:sess_1", "run_type": "agent", "status": "completed",
                "start_time": "2026-06-22T10:00:00+00:00", "end_time": "2026-06-22T10:00:01+00:00",
                "duration_ms": 1000, "metadata": {"session_id": "sess_1"},
                "outputs": {"success": True, "done_reason": "completed"},
            },
        },
        {
            "phase": "end",
            "run": {
                "id": "llm", "trace_id": "trace-1", "parent_run_id": "root",
                "name": "llm", "run_type": "llm", "status": "completed",
                "start_time": "2026-06-22T10:00:00+00:00", "end_time": "2026-06-22T10:00:00.5+00:00",
                "outputs": {"finish_reason": "stop", "has_tool_calls": False,
                            "response_metadata": {"model": "qwen3.7-max"}},
            },
        },
    ]
    _write(path, records)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n{invalid")

    assert len(load_completed_runs(path)) == 2
    summaries = list_trace_summaries(path)
    assert summaries[0]["stop_without_tools"] == 1
    assert summaries[0]["response_models"] == ["qwen3.7-max"]
    compact = get_trace("trace-1", path)
    assert compact["runs"][0]["id"] == "root"
    assert compact["runs"][0]["inputs"] == {}
    assert compact["runs"][1]["outputs"]["finish_reason"] == "stop"
    assert get_trace_run("trace-1", "llm", path)["outputs"]["response_metadata"]["model"] == "qwen3.7-max"
    assert get_trace("missing", path) is None


def test_trace_store_lists_recent_traces_without_full_file_scan(tmp_path):
    path = tmp_path / "traces.jsonl"
    records = []
    for idx in range(3):
        trace_id = f"trace-{idx}"
        root_id = f"root-{idx}"
        records.extend([
            {
                "phase": "end",
                "run": {
                    "id": f"llm-{idx}", "trace_id": trace_id, "parent_run_id": root_id,
                    "name": "llm", "run_type": "llm", "status": "completed",
                    "start_time": f"2026-06-22T10:0{idx}:00+00:00",
                    "end_time": f"2026-06-22T10:0{idx}:01+00:00",
                    "outputs": {"finish_reason": "stop", "has_tool_calls": False},
                },
            },
            {
                "phase": "end",
                "run": {
                    "id": root_id, "trace_id": trace_id, "parent_run_id": None,
                    "name": f"session:sess_{idx}", "run_type": "agent", "status": "completed",
                    "start_time": f"2026-06-22T10:0{idx}:00+00:00",
                    "end_time": f"2026-06-22T10:0{idx}:02+00:00",
                    "duration_ms": 2000,
                    "metadata": {"session_id": f"sess_{idx}"},
                    "outputs": {"success": True},
                },
            },
        ])
    _write(path, records)

    recent_runs = load_recent_completed_runs(path, target_traces=1)
    assert {run["trace_id"] for run in recent_runs} == {"trace-2"}
    assert {run["id"] for run in recent_runs} == {"root-2", "llm-2"}

    summaries = list_trace_summaries(path, limit=1)
    assert [summary["trace_id"] for summary in summaries] == ["trace-2"]
    assert summaries[0]["llm_run_count"] == 1
    assert get_trace("trace-2", path)["runs"][0]["id"] == "root-2"
    assert get_trace_run("trace-2", "root-2", path)["metadata"]["session_id"] == "sess_2"
