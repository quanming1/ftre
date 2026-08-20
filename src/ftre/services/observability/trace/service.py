from __future__ import annotations

from typing import Any

from ftre.trace_store import (
    SQLiteTraceExporter,
    get_trace,
    get_trace_run,
    list_trace_summaries,
)


class TraceService:
    key = "traces"

    def __init__(self, store: SQLiteTraceExporter | None = None) -> None:
        self.store = store or SQLiteTraceExporter()

    def list(self, **kwargs):
        return list_trace_summaries(path=self.store.path, **kwargs)

    def get(self, trace_id: str):
        return get_trace(trace_id, path=self.store.path)

    def get_run(self, trace_id: str, run_id: str):
        return get_trace_run(trace_id, run_id, path=self.store.path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)
